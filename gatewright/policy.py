# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Policy as code. Rules compare a metric to a value; no code is evaluated.

Available metrics (per gate, or per run if the rule has no `gates`):
    findings.critical / .high / .medium / .low / .info / .total   (unsuppressed)
    findings.suppressed
    jobs.failed / jobs.error / jobs.total
    any metric a parser reports, e.g. coverage.percent, tests.failed, lint.issues

Percent metrics (coverage.percent) from several jobs: the LOWEST counts, so every coverage report
has to meet a minimum. Counters (tests.failed, lint.issues) are added up.

`coverage:` in pipeline.yml (per gate, and top level for the run) becomes rules here too; see
coverage_rules(). They fail when no job measured coverage, unless `missing: ignore`.
"""

from __future__ import annotations

import operator

from .models import SEVERITIES, ERROR, FAILED, CoverageRule, JobResult, Pipeline, PolicyRule

OPS = {"==": operator.eq, "!=": operator.ne, "<": operator.lt, "<=": operator.le,
       ">": operator.gt, ">=": operator.ge}


def metrics_for(jobs: list[JobResult]) -> dict[str, float]:
    """Collect the metrics policies can use from a list of job results."""
    m: dict[str, float] = {f"findings.{s}": 0 for s in SEVERITIES}
    m["findings.total"] = 0
    m["findings.suppressed"] = 0
    m["jobs.total"] = len(jobs)
    m["jobs.failed"] = sum(1 for j in jobs if j.status == FAILED)
    m["jobs.error"] = sum(1 for j in jobs if j.status == ERROR)
    for j in jobs:
        for e in j.errors:
            if e.is_infra:
                continue
            if e.suppressed:
                m["findings.suppressed"] += 1
                continue
            m[f"findings.{e.severity}"] += 1
            m["findings.total"] += 1
        for k, v in j.metrics.items():
            # percent metrics: the lowest (deterministic, and every report must meet the bar); counters: sum
            if k.endswith("percent"):
                m[k] = min(v, m[k]) if k in m else v
            else:
                m[k] = m.get(k, 0) + v
    return m


COVERAGE_METRIC = "coverage.percent"


def coverage_rules(p: Pipeline, overrides: dict[str, float] | None = None) -> list[PolicyRule]:
    """Policy rules for the `coverage:` settings. `overrides` comes from `--coverage`:
    {"": 85} for the whole run, {"test": 90} for a gate. An override replaces the configured
    minimum (and keeps its `missing:` setting)."""
    overrides = overrides or {}
    rules = []
    targets: list[tuple[str, CoverageRule | None]] = [("", p.coverage)] + [(g.id, g.coverage) for g in p.gates]
    for gid, rule in targets:
        from_cli = gid in overrides
        if from_cli:
            rule = CoverageRule(float(overrides[gid]), rule.missing if rule else "fail")
        if rule is None:
            continue
        where = f"gate `{gid}`" if gid else "the whole run"
        source = "--coverage on the command line" if from_cli else "`coverage:` in pipeline.yml"
        rules.append(PolicyRule(id=f"min-coverage-{gid}" if gid else "min-coverage", metric=COVERAGE_METRIC,
                                op=">=", value=rule.min, gates=[gid] if gid else [],
                                description=f"Minimum coverage for {where} (set by {source}).",
                                missing="fail" if rule.missing == "fail" else "skip"))
    return rules


def evaluate(rules: list[PolicyRule], jobs: list[JobResult], gate: str | None, partial: bool = False) -> list[dict]:
    """Evaluate rules for one gate (`gate='commit'`) or for the whole run (`gate=None`).

    A rule whose metric no job produced is skipped, unless the rule says `missing="fail"`
    (coverage minimums). `partial`: only part of the pipeline was selected (`-j`, `-c`, or not
    all gates), so a missing metric is expected and is skipped, not failed.
    """
    m = metrics_for(jobs)
    out = []
    for r in rules:
        if gate is not None and r.gates and gate not in r.gates:
            continue
        if gate is not None and not r.gates:
            continue  # run-level rule, evaluated at the end
        if gate is None and r.gates:
            continue
        if r.metric not in m and r.missing == "fail" and not partial:
            where = f"gate `{gate}`" if gate else "this run"
            out.append({"id": r.id, "metric": r.metric, "op": r.op, "expected": r.value, "actual": None,
                        "passed": False, "skipped": False, "severity": r.severity,
                        "human": f"Policy `{r.id}` FAILED: no job in {where} measured `{r.metric}`, so the "
                                 f"required {r.op} {r.value:g} can't be checked. Add a job with "
                                 f"`parser: coverage`, or set `missing: ignore`.",
                        "description": r.description})
            continue
        if r.metric not in m:
            if partial and r.missing == "fail":
                human = (f"Policy `{r.id}` skipped: `{r.metric}` wasn't measured because only part of "
                         "the pipeline was selected.")
            else:
                human = f"Policy `{r.id}` skipped: metric `{r.metric}` was not produced by any job."
            out.append({"id": r.id, "metric": r.metric, "op": r.op, "expected": r.value, "actual": None,
                        "passed": True, "skipped": True, "severity": r.severity,
                        "human": human,
                        "description": r.description})
            continue
        actual = m[r.metric]
        passed = OPS[r.op](actual, r.value)
        human = (f"Policy `{r.id}` {'passed' if passed else 'FAILED'}: {r.metric} is {actual:g}, "
                 f"required {r.op} {r.value:g}.") + (f" {r.description}" if r.description else "")
        out.append({"id": r.id, "metric": r.metric, "op": r.op, "expected": r.value, "actual": actual,
                    "passed": passed, "skipped": False, "severity": r.severity, "human": human,
                    "description": r.description})
    return out
