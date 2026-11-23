# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""`coverage:` minimums per gate and for the whole run, and `--coverage` on the command line."""

import json
from pathlib import Path

import pytest

from gatewright import cli
from gatewright.config import ConfigError, load_pipeline
from gatewright.engine import Engine, Selection
from gatewright.models import JobResult
from gatewright.policy import coverage_rules, evaluate, metrics_for

from .conftest import PY  # noqa: E402

# A "coverage tool": writes a coverage.py JSON report with the percent from a file (default 80).
COV = """\
import json, os, sys
pct = float(open(sys.argv[1]).read()) if os.path.exists(sys.argv[1]) else 80.0
json.dump({"totals": {"percent_covered": pct}}, open(os.environ["GATEWRIGHT_OUT"] + "/cov.json", "w"))
"""


def cov_job(jid, gate, pct_file):
    return (f"  - {{id: {jid}, gate: {gate}, category: dev, run: '{PY} cov.py {pct_file}', cache: false,\n"
            f"     parser: {{type: coverage, file: $GATEWRIGHT_OUT/cov.json}}}}\n")


def pipeline(test_cov="", run_cov="", test_mode="enforced", extra_gate=""):
    return (f"version: 1\nname: t\n{run_cov}gates:\n  - id: commit\n  - id: test\n    mode: {test_mode}\n"
            f"{test_cov}{extra_gate}")


JOBS = ("jobs:\n"
        f"  - {{id: lint, gate: commit, category: dev, run: '{PY} -c \"print(1)\"', cache: false}}\n"
        + cov_job("unit-cov", "test", "unit.pct"))


@pytest.fixture
def make(project):
    def _make(pipe, jobs=JOBS, unit=None, **files):
        root = project(pipe, jobs, files={"cov.py": COV, **files})
        if unit is not None:
            (root / "unit.pct").write_text(str(unit))
        return root
    return _make


def run(root, **sel):
    return Engine(load_pipeline(root)).run(Selection(reports=["md"], **sel))


def gate(r, gid):
    return next(g for g in r.gates if g.gate == gid)


def rule(results, rid):
    return next(p for p in results if p["id"] == rid)


# ---------------------------------------------------------------- config
@pytest.mark.parametrize("value, expect", [("80", (80.0, "fail")), ("{min: 72.5}", (72.5, "fail")),
                                           ("{min: 60, missing: ignore}", (60.0, "ignore")), ("0", (0.0, "fail"))])
def test_config_forms(make, value, expect):
    p = load_pipeline(make(pipeline(f"    coverage: {value}\n", f"coverage: {value}\n")))
    assert (p.gate("test").coverage.min, p.gate("test").coverage.missing) == expect
    assert (p.coverage.min, p.coverage.missing) == expect
    assert p.gate("commit").coverage is None


@pytest.mark.parametrize("value, words", [("101", "between 0 and 100, not 101"), ("-1", "between 0 and 100"), ("true", "must be a number"), ("high", "must be a number"),
                                          ("{min: 80, max: 90}", "unknown key(s) max"), ("{missing: fail}", "missing `min:`"),
                                          ("{min: 80, missing: warn}", "must be fail or ignore"), ("{min: x}", "`min:` must be a number")])
def test_invalid_config_is_rejected(make, value, words):
    with pytest.raises(ConfigError) as e:
        load_pipeline(make(pipeline(f"    coverage: {value}\n")))
    assert "coverage" in "\n".join(e.value.problems) and words in "\n".join(e.value.problems)


def test_no_coverage_setting_means_no_rule(make):
    r = run(make(pipeline(), unit=10))
    assert r.status == "passed" and not any(p["id"].startswith("min-coverage") for p in r.policy)


# ---------------------------------------------------------------- gate minimum
def test_gate_minimum_passes_and_fails(make):
    root = make(pipeline("    coverage: 80\n"), unit=85)
    r = run(root)
    assert r.status == "passed" and rule(gate(r, "test").policy, "min-coverage-test")["passed"]
    (root / "unit.pct").write_text("79.9")
    r = run(root)
    g = gate(r, "test")
    p = rule(g.policy, "min-coverage-test")
    assert r.status == "failed" and g.status == "failed" and not p["passed"]
    assert "coverage.percent is 79.9, required >= 80" in p["human"]
    assert "min-coverage-test" in (Path(r.artifact_dir) / "report.md").read_text(encoding="utf-8")


def test_exactly_the_minimum_passes(make):
    assert run(make(pipeline("    coverage: 80\n"), unit=80)).status == "passed"


def test_advisory_gate_records_but_does_not_block(make):
    later = "  - id: deploy\n"
    jobs = JOBS + f"  - {{id: ship, gate: deploy, run: '{PY} -c \"print(2)\"', cache: false}}\n"
    r = run(make(pipeline("    coverage: 90\n", test_mode="advisory", extra_gate=later), jobs, unit=50))
    assert gate(r, "test").status == "failed" and gate(r, "deploy").status == "passed" and r.status == "warn"


def test_enforced_gate_blocks_later_gates(make):
    later = "  - id: deploy\n"
    jobs = JOBS + f"  - {{id: ship, gate: deploy, run: '{PY} -c \"print(2)\"', cache: false}}\n"
    r = run(make(pipeline("    coverage: 90\n", extra_gate=later), jobs, unit=50))
    assert gate(r, "deploy").status == "blocked"


def test_missing_measurement_fails_by_default_and_can_be_ignored(make):
    jobs = f"jobs:\n  - {{id: unit, gate: test, run: '{PY} -c \"print(1)\"', cache: false}}\n"
    r = run(make(pipeline("    coverage: 80\n"), jobs))
    p = rule(gate(r, "test").policy, "min-coverage-test")
    assert r.status == "failed" and not p["passed"] and "no job in gate `test` measured" in p["human"]
    r = run(make(pipeline("    coverage: {min: 80, missing: ignore}\n"), jobs))
    p = rule(gate(r, "test").policy, "min-coverage-test")
    assert r.status == "passed" and p["skipped"]


def test_skipped_optional_coverage_tool_does_not_pass_silently(make):
    jobs = "jobs:\n  - {id: cov, gate: test, run: 'not-installed-cov-xyz', optional: true, parser: coverage}\n"
    r = run(make(pipeline("    coverage: 80\n"), jobs))
    assert r.status == "failed" and gate(r, "test").status == "failed"


# ---------------------------------------------------------------- run minimum
def test_run_minimum_uses_the_lowest_report(make):
    jobs = JOBS + cov_job("e2e-cov", "commit", "e2e.pct")
    root = make(pipeline(run_cov="coverage: 70\n"), jobs, unit=95, **{"e2e.pct": "65"})
    r = run(root)
    p = rule(r.policy, "min-coverage")
    assert r.status == "failed" and p["actual"] == 65 and not p["passed"]
    (root / "e2e.pct").write_text("71")
    assert run(root).status == "passed"


def test_run_and_gate_minimums_together(make):
    r = run(make(pipeline("    coverage: 90\n", "coverage: 60\n"), unit=75))
    assert rule(r.policy, "min-coverage")["passed"] and not rule(gate(r, "test").policy, "min-coverage-test")["passed"]
    assert r.status == "failed"


# ---------------------------------------------------------------- partial runs
@pytest.mark.parametrize("sel", [{"gates": ["commit"]}, {"jobs": ["lint"]}, {"categories": ["sec"]}])
def test_partial_runs_skip_minimums_without_a_measurement(make, sel):
    """A pre-push hook running only `commit` must not fail for coverage it never measured."""
    r = run(make(pipeline("    coverage: 80\n", "coverage: 80\n"), unit=10), **sel)
    assert r.status in ("passed", "skipped")
    assert all(p["skipped"] for p in r.policy if p["id"] == "min-coverage")
    assert any("only part of the pipeline" in p["human"] for p in r.policy) or not r.policy


def test_partial_run_still_checks_a_measurement_it_has(make):
    r = run(make(pipeline("    coverage: 80\n"), unit=10), jobs=["unit-cov"])
    assert r.status == "failed"


def test_cancelled_run_gets_no_coverage_verdict(make):
    root = make(pipeline("    coverage: 80\n", "coverage: 80\n"), unit=10)
    eng = Engine(load_pipeline(root), lambda k, d: eng.stop.set() if k == "job_start" else None)
    r = eng.run(Selection(reports=["md"]))
    assert r.status == "cancelled" and r.policy == [] and all(g.policy == [] for g in r.gates)


# ---------------------------------------------------------------- --coverage
def test_cli_override_replaces_config_and_keeps_missing(make):
    root = make(pipeline("    coverage: {min: 50, missing: ignore}\n"), unit=60)
    assert run(root).status == "passed"
    r = run(root, coverage={"test": 70})
    p = rule(gate(r, "test").policy, "min-coverage-test")
    assert r.status == "failed" and p["expected"] == 70 and "--coverage" in p["description"]
    rules = coverage_rules(load_pipeline(root), {"test": 70})
    assert rules[0].missing == "skip"                          # `missing: ignore` from the file is kept


def test_cli_override_without_config(make, capsys):
    root = make(pipeline(), unit=60)
    assert cli.main(["-C", str(root), "run", "--report", "md", "--coverage", "70"]) == 1
    assert "min-coverage" in capsys.readouterr().out
    assert cli.main(["-C", str(root), "run", "--report", "md", "--coverage", "test=55", "--coverage", "50"]) == 0


@pytest.mark.parametrize("arg, words", [("test=101", "between 0 and 100"), ("=5", "GATE=PERCENT"),
                                        ("test=abc", "not a number"), ("nope=50", "unknown gate"),
                                        ("-3", "between 0 and 100")])
def test_bad_cli_values_exit_2_and_leave_no_run_folder(make, capsys, arg, words):
    root = make(pipeline(), unit=60)
    assert cli.main(["-C", str(root), "run", f"--coverage={arg}"]) == 2
    assert words in capsys.readouterr().err
    runs = root / ".gatewright" / "runs"
    assert not runs.exists() or not any(runs.iterdir())       # regression: empty run folders were left


def test_engine_rejects_bad_override_values(make):
    eng = Engine(load_pipeline(make(pipeline())))
    with pytest.raises(ValueError, match="between 0 and 100"):
        eng.rules(Selection(coverage={"": 150}))


def test_rerun_failed_keeps_the_override(make):
    root = make(pipeline(), unit=60)
    first = run(root, coverage={"test": 70})
    assert first.status == "failed" and first.selection["coverage"] == {"test": 70}
    again = Engine(load_pipeline(root)).rerun_failed(first.run_id)
    assert rule(gate(again, "test").policy, "min-coverage-test")["expected"] == 70


# ---------------------------------------------------------------- plan, TUI, template
def test_plan_shows_minimums_and_warns(make, capsys):
    jobs = f"jobs:\n  - {{id: unit, gate: test, run: '{PY} -c \"print(1)\"'}}\n"
    root = make(pipeline("    coverage: 80\n", "coverage: 75\n"), jobs)
    assert cli.main(["-C", str(root), "plan"]) == 0
    out = capsys.readouterr().out.replace("\n", " ")
    assert "Whole run needs coverage ≥ 75%" in out and "coverage ≥ 80%" in out
    assert "gate `test` requires coverage >= 80%" in out and "no job uses `parser: coverage`" in out
    root2 = make(pipeline("    coverage: {min: 80, missing: ignore}\n"), jobs)
    plan = Engine(load_pipeline(root2)).plan(Selection())
    assert plan.warnings == [] and plan.gates[0].coverage_min == 80 and plan.coverage_min is None
    plan = Engine(load_pipeline(root2)).plan(Selection(gates=["commit"]))   # gate not selected: no warning
    assert plan.warnings == []


def test_tui_dry_run_shows_the_minimum(make):
    import asyncio

    from gatewright.tui import GatewrightApp
    root = make(pipeline("    coverage: 80\n"), unit=90)

    async def scenario():
        app = GatewrightApp(load_pipeline(root))
        lines = []
        async with app.run_test(size=(150, 46)) as pilot:
            await pilot.pause(0.2)
            app._write_log = lambda text, style="": lines.append(str(text))
            app.action_dry_run()
        return lines
    assert any("coverage ≥ 80%" in line for line in asyncio.run(scenario()))


def test_python_template_sets_a_gate_minimum(tmp_path):
    from gatewright.templates import init
    init(tmp_path, "python")
    p = load_pipeline(tmp_path)
    assert p.gate("test").coverage.min == 70 and not any(r.metric == "coverage.percent" for r in p.policy)
    cov = p.job("coverage")
    assert cov.parser["type"] == "coverage" and not cov.optional     # the minimum needs a measurement


# ---------------------------------------------------------------- policy helpers
def test_percent_metrics_take_the_lowest_counters_add_up():
    a, b = JobResult("a", "g", "dev", "passed"), JobResult("b", "g", "dev", "passed")
    a.metrics, b.metrics = {"coverage.percent": 90, "tests.total": 3}, {"coverage.percent": 70, "tests.total": 2}
    m = metrics_for([a, b])
    assert m["coverage.percent"] == 70 and m["tests.total"] == 5


def test_evaluate_keeps_ordinary_rules_skippable(make):
    p = load_pipeline(make(pipeline() + "policy:\n  - {id: tests, metric: tests.total, op: '>', value: 0}\n"))
    res = evaluate(p.policy, [], None)
    assert res[0]["skipped"] and "was not produced" in res[0]["human"]


def test_history_and_manifest_keep_the_rules(make):
    r = run(make(pipeline("    coverage: 80\n"), unit=85))
    manifest = json.loads((Path(r.artifact_dir) / "manifest.json").read_text(encoding="utf-8"))
    test_gate = next(g for g in manifest["gates"] if g["gate"] == "test")
    assert any(p["id"] == "min-coverage-test" and p["passed"] for p in test_gate["policy"])


def test_invalid_top_level_value_and_null(make):
    with pytest.raises(ConfigError) as e:
        load_pipeline(make(pipeline(run_cov="coverage: 120\n")))
    assert "pipeline.yml: coverage: the value must be between 0 and 100, not 120" in e.value.problems[0]
    assert load_pipeline(make(pipeline(run_cov="coverage:\n"))).coverage is None   # empty = not set


def test_gate_minimum_without_jobs_is_a_config_error(make):
    """Regression: a gate with a minimum but no jobs never ran, so its minimum was never checked."""
    pipe = pipeline("    coverage: 80\n", extra_gate="  - id: empty\n    coverage: 60\n")
    with pytest.raises(ConfigError, match="gate 'empty' sets `coverage: 60` but has no jobs"):
        load_pipeline(make(pipe))
    ok = pipeline(extra_gate="  - id: empty\n    coverage: {min: 60, missing: ignore}\n")
    assert load_pipeline(make(ok)).gate("empty").coverage.min == 60


def test_override_for_a_gate_without_jobs_is_refused(make):
    root = make(pipeline(extra_gate="  - id: empty\n"), unit=90)
    with pytest.raises(ValueError, match="has no jobs"):
        run(root, coverage={"empty": 50})


def test_plan_ignores_a_user_policy_with_the_same_name(make):
    pipe = pipeline() + "policy:\n  - {id: min-coverage-test, gates: [test], metric: tests.total, op: '>', value: 0}\n"
    plan = Engine(load_pipeline(make(pipe))).plan(Selection())
    assert next(g for g in plan.gates if g.gate == "test").coverage_min is None


def test_release_notes_report_the_lowest_coverage(make):
    from gatewright.releasenotes import quality
    jobs = JOBS + cov_job("e2e-cov", "commit", "e2e.pct")
    r = run(make(pipeline(), jobs, unit=95, **{"e2e.pct": "65"}))
    assert "- Coverage: 65.0%" in quality(r)                     # regression: showed whichever came last


def test_random_values_and_results_agree_with_the_rules():
    """Fuzz (fixed seed): validation and pass/fail must match the documented rules for any input."""
    import random

    from gatewright.config import _coverage_problem
    from gatewright.models import Gate, Pipeline
    rnd = random.Random(1234)
    pool = [0, 100, 50, 99.99, 100.0001, -0.0001, -5, 250, True, False, None, "80", "high", [80], 1e9,
            float("nan"), {"min": 80}, {"min": 80, "missing": "ignore"}, {"min": "80"}, {"min": 80, "x": 1},
            {"missing": "fail"}, {"min": 101}, {"min": 0, "missing": "fail"}, {"min": 30, "missing": "warn"}]
    for _ in range(300):
        v = rnd.choice(pool + [rnd.uniform(-20, 120)])
        why = _coverage_problem(v)
        raw = v.get("min") if isinstance(v, dict) else v
        valid_number = isinstance(raw, (int, float)) and not isinstance(raw, bool) and 0 <= raw <= 100
        valid_shape = not isinstance(v, dict) or (set(v) <= {"min", "missing"} and "min" in v
                                                  and v.get("missing", "fail") in ("fail", "ignore"))
        assert (why == "") == (valid_number and valid_shape), (v, why)
        if why:
            continue
        from gatewright.config import coverage_rule
        cr = coverage_rule(v)
        p = Pipeline(name="t", root=".", gates=[Gate(id="test", coverage=cr)], jobs=[])
        measured = rnd.choice([None, rnd.uniform(0, 100), cr.min])
        j = JobResult("c", "test", "dev", "passed")
        if measured is not None:
            j.metrics = {"coverage.percent": measured}
        res = evaluate(coverage_rules(p), [j], "test")[0]
        if measured is None:
            assert res["passed"] is (cr.missing == "ignore") and res["skipped"] is (cr.missing == "ignore")
        else:
            assert res["passed"] is (measured >= cr.min) and not res["skipped"]


@pytest.mark.parametrize("gates", ["5", "nope", "{a: 1}"])
def test_broken_gates_value_is_reported_not_crashing(make, gates):
    """Regression: the coverage check iterated `gates: 5` and crashed with TypeError."""
    with pytest.raises(ConfigError):
        load_pipeline(make(f"version: 1\ncoverage: 80\ngates: {gates}\n"))


def test_plan_has_no_warning_for_unselected_gate_with_required_minimum(make):
    jobs = JOBS + f"  - {{id: e2e, gate: e2e, run: '{PY} -c \"1\"'}}\n"
    root = make(pipeline(extra_gate="  - id: e2e\n    coverage: 80\n"), jobs, unit=90)
    assert Engine(load_pipeline(root)).plan(Selection(gates=["test"])).warnings == []
    assert len(Engine(load_pipeline(root)).plan(Selection()).warnings) == 1


def test_plan_no_warning_when_the_run_minimum_can_be_measured(make):
    root = make(pipeline("    coverage: 80\n", "coverage: 70\n"), unit=90)
    assert Engine(load_pipeline(root)).plan(Selection()).warnings == []
