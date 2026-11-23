# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The engine: the one place where a pipeline run is orchestrated.

Every front-end (the CLI in cli.py, the terminal UI in tui.py, and later the
git triggers) only builds a `Selection` and calls `Engine.run()` / `Engine.plan()`.
Nothing else starts jobs. That keeps behaviour identical everywhere.

Flow of `Engine.run()`:

    detect hardware -> pick profile -> make budget          (hardware.py)
    read git commit/branch                                  (git_info)
    choose gates + jobs from the Selection                  (_selected_gates/_selected_jobs)
    for each gate, in pipeline order:
        check `requires` (earlier gate passed for this commit?)
        Scheduler.run(jobs) -> for each job: reuse | cache hit | runner.execute()
        evaluate gate policy -> gate status
        enforced gate failed -> every later gate is BLOCKED
    evaluate run policy -> run status
    finalize(): reports, manifest.json, checksums, .tar.gz, SQLite history

Progress is reported through `on_event(kind, data)` callbacks (run_start,
gate_start, job_start, job_waiting, job_log, job_end, gate_end, run_end,
resources, message). The CLI prints them; the TUI updates widgets.
To build another front-end (web UI, chat bot...), subscribe to these events.
"""

from __future__ import annotations

import secrets as pysecrets
import subprocess  # nosec B404 - git metadata only
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import artifacts, cleanup, history as hist, policy as pol, reports
from .baseline import Baseline
from .cache import Cache
from .graph import levels, with_dependencies
from .hardware import detect, make_budget, pick_profile
from .models import (BLOCKED, CACHED, CANCELLED, ERROR, FAILED, PASSED, REUSED, SKIPPED, WARN, GateResult, Job,
                     JobResult, Pipeline, PolicyRule, RunResult, fmt_bytes)
from .runner import JobHandle, execute, mask_output_files, tool_version
from .scheduler import Scheduler
from .secrets import SecretStore

EventFn = Callable[[str, dict], None]


@dataclass
class Selection:
    """What to run. Built by the CLI flags or the TUI checkboxes.

    The selection is stored in the run (manifest + history) so `rerun-failed`
    can repeat exactly the same run.
    """
    gates: list[str] | None = None        # None = all enabled gates
    categories: list[str] | None = None   # dev / sec / ops
    jobs: list[str] | None = None         # explicit job ids (dependencies are added)
    use_cache: bool = True
    mode_override: str | None = None      # force every gate to enforced/advisory
    reports: list[str] = field(default_factory=lambda: ["md", "html", "sarif", "junit"])
    coverage: dict[str, float] = field(default_factory=dict)   # --coverage: {"": run minimum, "<gate>": gate minimum}

    def to_dict(self) -> dict:
        return {"gates": self.gates, "categories": self.categories, "jobs": self.jobs,
                "use_cache": self.use_cache, "mode_override": self.mode_override, "reports": self.reports,
                "coverage": self.coverage}

    @classmethod
    def from_dict(cls, d: dict) -> "Selection":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class PlannedGate:
    """One gate in a dry-run plan: its jobs grouped into parallel waves."""
    gate: str
    mode: str
    waves: list[list[Job]]
    blocked_reason: str = ""
    coverage_min: float | None = None   # minimum coverage for this gate, if one is set

    @property
    def jobs(self) -> list[Job]:
        return [j for w in self.waves for j in w]


@dataclass
class Plan:
    """Result of `Engine.plan()`: what would run, with which budget, on which machine."""
    gates: list[PlannedGate]
    profile: str
    budget: dict
    hardware: dict
    warnings: list[str]
    coverage_min: float | None = None   # minimum coverage for the whole run, if one is set


def git_info(root: str) -> tuple[str, str, bool]:
    """Return (commit sha, branch, dirty) for the project, or ('', '', False) outside git.

    Git is optional: without it runs work, but commit tracking and gate
    `requires` (which needs a commit to look up earlier runs) are disabled.
    """
    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,  # nosec B603 B607
                                  timeout=10, check=False).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    commit = git("rev-parse", "--verify", "-q", "HEAD")   # without --verify: "HEAD" in a repository without commits
    if not commit:
        return "", "", False
    return commit, git("rev-parse", "--abbrev-ref", "HEAD"), bool(git("status", "--porcelain", "--", "."))


def new_run_id() -> str:
    """Sortable, human-readable run id: YYYYMMDD-HHMMSS-<4 hex>."""
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + pysecrets.token_hex(2)


class Engine:
    """Orchestrates runs for one loaded Pipeline. Create one per pipeline.

    `on_event(kind, data)` receives progress events (see module docstring).
    `stop` is a threading.Event: set it to cancel a running pipeline cleanly.
    """
    def __init__(self, pipeline: Pipeline, on_event: EventFn | None = None, data_root: str | None = None,
                 trusted: bool = True):
        self.p = pipeline
        self.root = pipeline.root                    # where the code is and jobs run
        self.data_root = data_root or pipeline.root  # where .gatewright/ state lives (runs, history, cache, secrets)
        self.trusted = trusted                       # False: secrets are withheld (e.g. pull requests from forks)
        self.on_event = on_event or (lambda k, d: None)
        self.stop = threading.Event()
        self.history = hist.History(self.data_root)

    # ------------------------------------------------------------------ select
    def _selected_gates(self, sel: Selection) -> list[str]:
        if sel.gates:
            unknown = [g for g in sel.gates if not self.p.gate(g)]
            if unknown:
                raise ValueError(f"Unknown gate(s): {', '.join(unknown)}. "
                                 f"Available: {', '.join(g.id for g in self.p.gates)}")
            return [g.id for g in self.p.gates if g.id in sel.gates]
        return [g.id for g in self.p.gates if g.enabled]

    def rules(self, sel: Selection) -> list[PolicyRule]:
        """pipeline.yml `policy:` plus the coverage minimums (`coverage:`, overridden by --coverage)."""
        for gid, v in sel.coverage.items():
            if gid and not self.p.gate(gid):
                raise ValueError(f"--coverage {gid}={v:g}: unknown gate `{gid}`. "
                                 f"Available: {', '.join(g.id for g in self.p.gates)}")
            if gid and not self.p.jobs_in(gid):
                raise ValueError(f"--coverage {gid}={v:g}: gate `{gid}` has no jobs, so nothing would be checked")
            if not 0 <= v <= 100:
                raise ValueError(f"--coverage {(gid + '=') if gid else ''}{v:g}: must be between 0 and 100")
        return self.p.policy + pol.coverage_rules(self.p, sel.coverage)

    def _partial(self, sel: Selection, gates: list[str]) -> tuple[bool, bool]:
        """(gate-level, run-level): did the selection leave out part of the pipeline? Then a coverage
        minimum without a measurement is skipped instead of failed (e.g. `-j lint`, a pre-push hook
        that runs only the commit gate)."""
        within = bool(sel.jobs or sel.categories)
        return within, within or set(gates) != {g.id for g in self.p.gates if g.enabled}

    def _selected_jobs(self, sel: Selection, gates: list[str]) -> list[Job]:
        jobs = [j for j in self.p.jobs if j.gate in gates]
        if sel.jobs:
            unknown = [j for j in sel.jobs if not self.p.job(j)]
            if unknown:
                raise ValueError(f"Unknown job(s): {', '.join(unknown)}")
            ids = with_dependencies(set(sel.jobs), self.p.jobs)
            jobs = [j for j in self.p.jobs if j.id in ids]
        if sel.categories:
            keep = {j.id for j in jobs if j.category in sel.categories}
            keep = with_dependencies(keep, jobs)
            jobs = [j for j in jobs if j.id in keep]
        return jobs

    def _mode(self, gid: str, sel: Selection) -> str:
        return sel.mode_override or self.p.gate(gid).mode

    # ------------------------------------------------------------------ plan
    def plan(self, sel: Selection | None = None) -> Plan:
        """Dry run: decide gates, jobs, waves and resource budget without running anything."""
        sel = sel or Selection()
        hw = detect(self.root)
        profile = pick_profile(hw, self.p.profile)
        budget = make_budget(hw, profile, self.p.limits)
        gates = self._selected_gates(sel)
        jobs = self._selected_jobs(sel, gates)
        rules = self.rules(sel)
        gate_ids = [g for g in [gg.id for gg in self.p.gates] if any(j.gate == g for j in jobs)]
        commit, _, _ = git_info(self.root)
        warnings = self._coverage_warnings(rules, gates, jobs)
        planned = []
        for gid in gate_ids:
            gj = [j for j in jobs if j.gate == gid]
            reason = self._requires_problem(gid, gate_ids, commit)
            planned.append(PlannedGate(gid, self._mode(gid, sel), levels(gj), reason,
                                       self._coverage_min(rules, gid)))
            for j in gj:
                if j.mem > budget.mem:
                    warnings.append(f"job `{j.id}` declares {fmt_bytes(j.mem)} RAM but the budget is "
                                    f"{fmt_bytes(budget.mem)}; it will run alone")
        return Plan(planned, profile, budget.to_dict(), hw.to_dict(), warnings, self._coverage_min(rules, None))

    @staticmethod
    def _coverage_min(rules: list[PolicyRule], gid: str | None) -> float | None:
        want = "min-coverage" + (f"-{gid}" if gid else "")
        return next((r.value for r in rules if r.id == want and r.metric == pol.COVERAGE_METRIC
                     and r.gates == ([gid] if gid else [])), None)

    @staticmethod
    def _measures_coverage(j: Job) -> bool:
        return j.parser.get("type") == "coverage"

    def _coverage_warnings(self, rules: list[PolicyRule], gates: list[str], jobs: list[Job]) -> list[str]:
        """Dry-run hints for coverage minimums that can't be met because nothing measures coverage."""
        out = []
        for r in rules:
            if r.metric != pol.COVERAGE_METRIC or r.missing != "fail":
                continue
            if r.gates:
                gid = r.gates[0]
                if gid in gates and not any(self._measures_coverage(j) for j in self.p.jobs_in(gid)):
                    out.append(f"gate `{gid}` requires coverage >= {r.value:g}%, but none of its jobs uses "
                               "`parser: coverage`, so the gate will fail (or set `missing: ignore`)")
            elif not any(self._measures_coverage(j) for j in self.p.jobs):
                out.append(f"the run requires coverage >= {r.value:g}%, but no job uses `parser: coverage`, "
                           "so full runs will fail (or set `missing: ignore`)")
        return out

    def _requires_problem(self, gid: str, gates_in_run: list[str], commit: str) -> str:
        g = self.p.gate(gid)
        missing = []
        for req in g.requires:
            if req in gates_in_run:
                continue
            if not commit or not self.history.gate_passed(req, commit):
                missing.append(req)
        if missing:
            return (f"gate `{gid}` requires gate(s) {', '.join(missing)} to have passed for this commit"
                    f"{' (' + commit[:8] + ')' if commit else ''}; run them first or include them in this run")
        return ""

    # ------------------------------------------------------------------ run
    def run(self, sel: Selection | None = None, resume_from: RunResult | None = None) -> RunResult:
        """Run the selected gates and jobs and return the finished RunResult.

        Always returns a result - also when jobs fail, are cancelled or crash -
        and always writes the artifact bundle (partial runs still get reports).
        `resume_from`: a previous RunResult whose passed jobs are reused (rerun-failed).
        """
        sel = sel or Selection()
        # validate the selection first: a bad --gates/-j/--coverage must not leave an empty run folder
        gates = self._selected_gates(sel)
        jobs = self._selected_jobs(sel, gates)
        rules = self.rules(sel)
        self.stop.clear()
        t0 = time.monotonic()
        hw = detect(self.root)
        profile = pick_profile(hw, self.p.profile)
        budget = make_budget(hw, profile, self.p.limits)
        commit, branch, dirty = git_info(self.root)
        run_id = new_run_id()
        run_dir = Path(self.data_root) / ".gatewright" / "runs" / run_id
        (run_dir / "logs").mkdir(parents=True, exist_ok=True)
        run = RunResult(run_id=run_id, pipeline=self.p.name, root=self.root,
                        started=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        commit=commit, branch=branch, dirty=dirty, profile=profile, hardware=hw.to_dict(),
                        selection=sel.to_dict(), artifact_dir=str(run_dir),
                        resumed_from=resume_from.run_id if resume_from else "")

        secrets = SecretStore(self.data_root, enabled=self.trusted)
        # an untrusted commit must not bring the list of its own findings to ignore
        baseline = Baseline(self.root if self.trusted else self.data_root)
        cache = Cache(self.root, cache_dir=Path(self.data_root) / ".gatewright" / "cache")
        run.warnings.extend(secrets.warnings + baseline.warnings)
        reuse: dict[str, JobResult] = {}
        if resume_from:
            if resume_from.commit and resume_from.commit != commit:
                run.warnings.append(f"resuming run {resume_from.run_id} made on commit {resume_from.commit[:8]}, "
                                    f"but HEAD is now {commit[:8]}; passed jobs are reused anyway")
            reuse = {j.job_id: j for j in resume_from.all_jobs() if j.status in (PASSED, WARN, CACHED, REUSED)}

        gate_partial, run_partial = self._partial(sel, gates)
        gate_ids = [g.id for g in self.p.gates if any(j.gate == g.id for j in jobs)]
        self.on_event("run_start", {"run": run, "budget": budget, "gates": gate_ids,
                                    "jobs": {g: [j.id for j in jobs if j.gate == g] for g in gate_ids}})

        done: dict[str, JobResult] = {}
        stop_reason = ""
        for gid in gate_ids:
            gjobs = [j for j in jobs if j.gate == gid]
            gres = GateResult(gate=gid, mode=self._mode(gid, sel))
            g_t0 = time.monotonic()
            self.on_event("gate_start", {"gate": gid, "mode": gres.mode, "jobs": [j.id for j in gjobs]})
            req = self._requires_problem(gid, gate_ids, commit)
            if req and gres.mode == "advisory":
                run.warnings.append(req)
                req = ""
            if stop_reason or req or self.stop.is_set():
                note = "cancelled" if self.stop.is_set() else (req or stop_reason)
                for j in gjobs:
                    r = JobResult(j.id, gid, j.category, BLOCKED if not self.stop.is_set() else SKIPPED, note=note)
                    gres.jobs.append(r)
                    self.on_event("job_end", {"result": r})
                gres.status = BLOCKED if not self.stop.is_set() else CANCELLED
                gres.note = note
                run.gates.append(gres)
                self.on_event("gate_end", {"gate": gres})
                continue

            def run_one(job: Job, handle: JobHandle) -> JobResult:
                if job.id in reuse:
                    r = JobResult.from_dict(reuse[job.id].to_dict())
                    r.status, r.note = REUSED, f"from run {resume_from.run_id}"
                    return r
                key = ""
                if sel.use_cache and job.cache and not job.image:
                    key = cache.key(job, tool_version(job.tool_name, self.root))
                    hit = cache.get(job, key)
                    if hit:
                        hit.status, hit.note, hit.duration = CACHED, "inputs unchanged", 0.0
                        return hit
                res = execute(job, root=self.root, run_id=run_id, out_dir=str(run_dir / "out" / job.id),
                              log_file=str(run_dir / "logs" / f"{job.id}.log"), secrets=secrets, handle=handle,
                              on_log=lambda jid, line: self.on_event("job_log", {"job_id": jid, "line": line}),
                              suppress=baseline.apply, nice=budget.nice)
                res.outputs = artifacts.collect_outputs(self.root, run_dir, job.id, job.outputs)
                if res.outputs:   # copied files go into the artifact too: mask secrets in them
                    mask_output_files(str(run_dir / "out" / job.id), secrets)
                if key:
                    res.cache_key = key
                    cache.put(job, key, res)
                return res

            sched = Scheduler(budget, self.on_event, self.stop)
            results = sched.run(gjobs, run_one, external=done)
            done.update(results)
            gres.jobs = [results[j.id] for j in gjobs if j.id in results]
            # a cancelled gate gets no policy verdict: it is "cancelled", not failed for missing coverage
            gres.policy = [] if self.stop.is_set() else pol.evaluate(rules, gres.jobs, gid, gate_partial)
            gres.status = self._gate_status(gres)
            gres.duration = round(time.monotonic() - g_t0, 2)
            if gres.status == FAILED and gres.mode == "enforced":
                stop_reason = f"enforced gate `{gid}` failed"
            run.gates.append(gres)
            self.on_event("gate_end", {"gate": gres})

        run.policy = [] if self.stop.is_set() else pol.evaluate(rules, run.all_jobs(), None, run_partial)
        for s, e in baseline.expired_hits:
            run.warnings.append(f"suppression for `{e.location or e.rule}` expired on {s.expires} "
                                f"({s.reason}); the finding is active again")
        run.tool_versions = {self.p.job(j.job_id).tool_name: j.tool_version for j in run.all_jobs()
                             if j.tool_version and self.p.job(j.job_id)}
        run.status = self._run_status(run)
        if not jobs:
            run.status = SKIPPED
            run.warnings.append("no jobs matched the selection (gates/categories/jobs), so nothing was run")
        if self.stop.is_set():
            run.status = CANCELLED
            run.warnings.append("the run was cancelled before it finished; it can't be promoted")
        run.finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
        run.duration = round(time.monotonic() - t0, 2)
        if self.p.notify.get("channels"):
            from .notify import Notifier
            notifier = Notifier(self.p.notify, SecretStore(self.data_root),
                                lambda m: self.on_event("message", {"level": "warning", "text": m}))
            notifier.run_finished(run)
            run.warnings.extend(notifier.warnings)
        self.finalize(run)
        self.on_event("run_end", {"run": run})
        return run

    @staticmethod
    def _gate_status(g: GateResult) -> str:
        if any(j.status in (FAILED, ERROR, BLOCKED) for j in g.jobs) or \
                any(not p["passed"] for p in g.policy):
            return FAILED
        if any(j.status == SKIPPED and j.note == "cancelled" for j in g.jobs):
            return CANCELLED   # incomplete: must never be reported as passed or skipped
        if g.jobs and all(j.status == SKIPPED for j in g.jobs):
            return SKIPPED
        if any(j.status == WARN for j in g.jobs):
            return WARN
        return PASSED

    @staticmethod
    def _run_status(run: RunResult) -> str:
        if any(g.status in (FAILED, BLOCKED) and g.mode == "enforced" for g in run.gates) or \
                any(not p["passed"] for p in run.policy):
            return FAILED
        if any(g.status in (FAILED, WARN, BLOCKED) for g in run.gates):
            return WARN
        if run.gates and all(g.status == SKIPPED for g in run.gates):
            return SKIPPED
        return PASSED

    # ------------------------------------------------------------------ outputs
    def finalize(self, run: RunResult) -> None:
        """Write everything a run produces: reports, manifest, checksums, archive, history.

        The diff against the previous run is computed *before* saving this run.
        """
        run_dir = Path(run.artifact_dir)
        prev = self.history.load("last")
        diff = None
        if prev and prev.run_id != run.run_id:
            d = hist.diff(prev, run)
            diff = {"prev_id": prev.run_id, **d}
        formats = run.selection.get("reports") or ["md"]
        written, warns = reports.write_reports(run, run_dir, formats, diff)
        run.warnings.extend(warns)
        if warns:  # re-render so the warning shows in the md/html
            reports.write_reports(run, run_dir, [f for f in formats if f != "pdf"], diff)
        artifacts.write_manifest(run, run_dir)
        artifacts.write_checksums(run_dir)
        self._sign(run, run_dir)
        artifacts.pack(run_dir)
        self.history.save(run)
        if self.p.keep_runs:
            for old in cleanup.old_runs(self.data_root, self.p.keep_runs):
                cleanup.delete(self.data_root, old)

    def _sign(self, run: RunResult, run_dir: Path) -> None:
        """Sign the artifact if `signing:` is configured (v0.6). A signing problem is a
        warning in the history, never a crash; `signing.required` makes it a loud one."""
        if not self.p.signing:
            return
        from . import signing
        cfg = signing.SigningConfig.from_dict(self.p.signing)
        if not cfg.auto:
            return
        try:
            meta = signing.sign(run_dir, cfg)
            self.on_event("message", {"level": "info", "text": f"artifact signed by {meta['identity']}"})
        except signing.SigningError as e:
            msg = f"artifact NOT signed: {e}"
            run.warnings.append(msg)
            self.on_event("message", {"level": "error" if cfg.required else "warning", "text": msg})

    def rerun_failed(self, run_id: str | None = None) -> RunResult:
        """Repeat a previous run's selection, reusing its passed jobs.

        `run_id` may be a full id, a prefix, 'last' or None (= last run).
        """
        prev = self.history.load(run_id)
        if not prev:
            raise ValueError(f"No run found{' with id ' + run_id if run_id else ''}.")
        sel = Selection.from_dict(prev.selection)
        return self.run(sel, resume_from=prev)
