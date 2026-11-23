# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Environments, promotion and rollback (v0.6).

An environment (staging, production, ...) is a deploy command plus rules about
WHICH artifacts may go there:

    environments:
      staging:
        deploy: ./scripts/deploy.sh staging      # gets $GATEWRIGHT_ARTIFACT etc.
        verify: ./scripts/smoke.sh staging       # optional health check after deploying
        requires: [test]                         # gates that must have passed in the run
        auto_rollback: true                      # verify failed -> redeploy the previous artifact
      production:
        deploy: ./scripts/deploy.sh production
        after: [staging]                         # the same run must have been deployed to staging first
        require_signature: true
        approval: true                           # ask "are you sure?" (or pass --yes)

`gatewright promote <run> --to <env>` checks the rules, runs `deploy` (through the
normal job runner: logs, timeout, secrets masking, human errors), then `verify`.
The SAME artifact moves from environment to environment; nothing is rebuilt.
Every attempt is recorded in the `deployments` table of .gatewright/gatewright.db,
and its logs are kept in .gatewright/deployments/<id>-<env>/.
"""

from __future__ import annotations

import getpass
import json
import re
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import artifacts, signing
from .history import History
from .locks import Locked, file_lock
from .models import ERROR, PASSED, WARN, Job, JobResult, Pipeline, RunResult
from .runner import JobHandle, execute
from .secrets import SecretStore

SUCCEEDED, DEPLOY_FAILED, VERIFY_FAILED, ROLLED_BACK = "succeeded", "failed", "verify_failed", "rolled_back"

SCHEMA = """
CREATE TABLE IF NOT EXISTS deployments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, env TEXT, run_id TEXT, commit_sha TEXT, kind TEXT,
    status TEXT, started TEXT, finished TEXT, by TEXT, previous_run_id TEXT, log_dir TEXT, note TEXT
);
"""


class DeployError(Exception):
    """A promotion/rollback was refused or could not start. The message says why and what to do."""


@dataclass
class Environment:
    """One entry of `environments:` in pipeline.yml."""
    name: str
    deploy: str
    verify: str = ""
    requires: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)
    require_signature: bool = False
    auto_rollback: bool = False
    approval: bool = False
    timeout: int = 1800
    secrets: list[str] = field(default_factory=list)
    env: dict = field(default_factory=dict)
    release_notes: bool = False
    shell: str = "auto"

    @classmethod
    def from_dict(cls, name: str, d: dict) -> "Environment":
        return cls(name=name, **{k: v for k, v in d.items() if k in cls.__dataclass_fields__ and k != "name"})


@dataclass
class Deployment:
    """One row of deployment history."""
    id: int
    env: str
    run_id: str
    commit_sha: str
    kind: str          # deploy | rollback
    status: str
    started: str
    finished: str
    by: str
    previous_run_id: str
    log_dir: str
    note: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Deployer:
    """Promote runs to environments and roll back, for one project."""

    def __init__(self, pipeline: Pipeline, log: Callable[[str], None] | None = None, notifier=None,
                 confirm: Callable[[str], bool] | None = None):
        self.p = pipeline
        self.root = pipeline.root
        self.envs = {n: Environment.from_dict(n, d) for n, d in (pipeline.environments or {}).items()}
        self.history = History(self.root)
        self.db = Path(self.root) / ".gatewright" / "gatewright.db"
        with closing(self._conn()) as c:
            c.executescript(SCHEMA)
        self.log = log or (lambda m: None)
        self.notifier = notifier
        self.confirm = confirm or _ask
        self.signing = signing.SigningConfig.from_dict(pipeline.signing)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db, timeout=15)
        c.row_factory = sqlite3.Row
        return c

    # ------------------------------------------------------------------ history
    def deployments(self, env: str | None = None, limit: int = 50) -> list[Deployment]:
        with closing(self._conn()) as c:
            if env:
                rows = c.execute("SELECT * FROM deployments WHERE env=? ORDER BY id DESC LIMIT ?", (env, limit))
            else:
                rows = c.execute("SELECT * FROM deployments ORDER BY id DESC LIMIT ?", (limit,))
            return [Deployment(**{k: (r[k] if r[k] is not None else "") for k in r.keys()}) for r in rows]

    def current(self, env: str) -> Deployment | None:
        """What is live in `env` now: the latest successful deploy or rollback."""
        return next((d for d in self.deployments(env, 500) if d.status == SUCCEEDED), None)

    def _record(self, env: str, run: RunResult, kind: str, by: str, previous: str, log_dir: Path) -> int:
        with closing(self._conn()) as c, c:
            cur = c.execute("INSERT INTO deployments (env, run_id, commit_sha, kind, status, started, by, "
                            "previous_run_id, log_dir) VALUES (?,?,?,?,?,?,?,?,?)",
                            (env, run.run_id, run.commit, kind, "running", _now(), by, previous, str(log_dir)))
            return cur.lastrowid

    def _finish(self, dep_id: int, status: str, note: str = "") -> None:
        with closing(self._conn()) as c, c:
            c.execute("UPDATE deployments SET status=?, finished=?, note=? WHERE id=?", (status, _now(), note, dep_id))

    # ------------------------------------------------------------------ checks
    def env(self, name: str) -> Environment:
        if name not in self.envs:
            known = ", ".join(self.envs) or "none (add `environments:` to pipeline.yml)"
            raise DeployError(f"Unknown environment `{name}`. Known: {known}.")
        return self.envs[name]

    def load_run(self, run_id: str | None) -> RunResult:
        run = self.history.load(run_id)
        if not run:
            raise DeployError(f"No run found{' for ' + run_id if run_id else ''}. See `gatewright history`.")
        return run

    def problems(self, run: RunResult, env: Environment, rollback: bool = False) -> list[str]:
        """Every reason this run may NOT go to this environment (empty = allowed)."""
        out = []
        if not rollback:
            if run.status not in (PASSED, WARN):
                out.append(f"run {run.run_id} is {run.status.upper()}; only passing runs can be promoted")
            gates = {g.gate: g for g in run.gates}
            for req in env.requires:
                g = gates.get(req)
                if g is None:
                    out.append(f"gate `{req}` was not part of run {run.run_id}")
                elif g.status not in (PASSED, WARN):
                    out.append(f"gate `{req}` did not pass in run {run.run_id} (it was {g.status})")
            for prev_env in env.after:
                if not any(d.run_id == run.run_id and d.status == SUCCEEDED for d in self.deployments(prev_env, 500)):
                    out.append(f"run {run.run_id} has not been deployed to `{prev_env}` yet "
                               f"(`{env.name}` comes after `{prev_env}`)")
        run_dir = Path(run.artifact_dir)
        if not run_dir.is_dir():
            out.append(f"the artifact folder {run_dir} is missing")
            return out
        bad = artifacts.verify(run_dir)
        if bad:
            out.append("the artifact was modified or is incomplete: " + "; ".join(bad[:3]))
        if env.require_signature or self.signing.required:
            ok, why = signing.verify(run_dir, self.signing.allowed_signers_path(self.root))
            if not ok:
                out.append(f"signature check failed: {why}")
        return out

    # ------------------------------------------------------------------ actions
    def _locked(self, env_name: str, action: Callable[[], Deployment]) -> Deployment:
        """One deployment per environment at a time, also between processes."""
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", self.env(env_name).name)
        try:
            with file_lock(Path(self.root) / ".gatewright" / "deployments" / f"{name}.lock"):
                return action()
        except Locked:
            raise DeployError(f"Another deployment to `{env_name}` is in progress. Wait for it to finish "
                              "(`gatewright deployments` shows it), then try again.") from None

    def promote(self, run_id: str | None, env_name: str, yes: bool = False, by: str = "") -> Deployment:
        """Deploy a run's artifact to an environment (checks, approval, deploy, verify, auto-rollback)."""
        return self._locked(env_name, lambda: self._promote(run_id, env_name, yes, by))

    def _promote(self, run_id: str | None, env_name: str, yes: bool, by: str) -> Deployment:
        env, run = self.env(env_name), self.load_run(run_id)
        issues = self.problems(run, env)
        if issues:
            raise DeployError(f"Run {run.run_id} cannot go to `{env.name}`:\n  - " + "\n  - ".join(issues))
        self._approve(env, f"Deploy run {run.run_id} (commit {run.commit[:8] or 'n/a'}) to {env.name}?", yes)
        return self._deploy(run, env, "deploy", by or _user())

    def rollback(self, env_name: str, to_run: str | None = None, yes: bool = False, by: str = "") -> Deployment:
        """Redeploy an earlier artifact: `to_run`, or the one live before the current one."""
        return self._locked(env_name, lambda: self._rollback(env_name, to_run, yes, by))

    def _rollback(self, env_name: str, to_run: str | None, yes: bool, by: str) -> Deployment:
        env = self.env(env_name)
        cur = self.current(env.name)
        if to_run:
            target = self.load_run(to_run)
            # a rollback skips the promotion checks, so it may only go back to something that was live here
            if not any(d.run_id == target.run_id and d.status == SUCCEEDED
                       for d in self.deployments(env.name, 500)):
                raise DeployError(f"Run {target.run_id} was never successfully deployed to `{env.name}`, so it "
                                  f"can't be a rollback target. Use `gatewright promote {target.run_id} "
                                  f"--to {env.name}` (that runs the checks).")
        else:
            # the newest successful deployment of a DIFFERENT artifact than what's live now
            prev = next((d for d in self.deployments(env.name, 500)
                         if d.status == SUCCEEDED and (cur is None or d.run_id != cur.run_id)), None)
            if prev is None:
                raise DeployError(f"Nothing to roll back to: `{env.name}` has no earlier successful deployment.")
            target = self.load_run(prev.run_id)
        issues = self.problems(target, env, rollback=True)
        if issues:
            raise DeployError(f"Cannot roll `{env.name}` back to {target.run_id}:\n  - " + "\n  - ".join(issues))
        self._approve(env, f"Roll {env.name} back to run {target.run_id} (commit {target.commit[:8] or 'n/a'})?", yes)
        return self._deploy(target, env, "rollback", by or _user())

    def _approve(self, env: Environment, question: str, yes: bool) -> None:
        if env.approval and not yes and not self.confirm(question):
            raise DeployError(f"Not confirmed. `{env.name}` requires approval: answer yes, or pass --yes.")

    def _deploy(self, run: RunResult, env: Environment, kind: str, by: str) -> Deployment:
        cur = self.current(env.name)
        log_dir = Path(self.root) / ".gatewright" / "deployments" / "pending"
        dep_id = self._record(env.name, run, kind, by, cur.run_id if cur else "", log_dir)
        log_dir = Path(self.root) / ".gatewright" / "deployments" / f"{dep_id}-{env.name}"
        log_dir.mkdir(parents=True, exist_ok=True)
        with closing(self._conn()) as c, c:
            c.execute("UPDATE deployments SET log_dir=? WHERE id=?", (str(log_dir), dep_id))
        self.log(f"▶ {kind} #{dep_id}: run {run.run_id} → {env.name}")

        if env.release_notes and run.commit:
            self._write_release_notes(run, env, cur, log_dir)

        res = self._step("deploy", env.deploy, run, env, dep_id, log_dir)
        if res.status not in (PASSED, WARN):
            note = _first_error(res)
            self._finish(dep_id, DEPLOY_FAILED, note)
            self.log(f"✗ deploy to {env.name} failed: {note}")
            if self.notifier:
                self.notifier.deployment("deploy_failed", env.name, run.run_id, f"{kind} of {run.run_id} failed: {note}")
            return self._get(dep_id)

        if env.verify:
            ver = self._step("verify", env.verify, run, env, dep_id, log_dir)
            if ver.status not in (PASSED, WARN):
                note = _first_error(ver)
                self.log(f"✗ verify of {env.name} failed: {note}")
                if env.auto_rollback and cur and cur.run_id != run.run_id and kind == "deploy":
                    self._finish(dep_id, ROLLED_BACK, f"verify failed: {note}; rolled back to {cur.run_id}")
                    self.log(f"↩ auto-rollback of {env.name} to {cur.run_id}")
                    back = self._deploy(self.load_run(cur.run_id), env, "rollback", "auto-rollback")
                    if self.notifier:
                        self.notifier.deployment("rolled_back", env.name, run.run_id,
                                                 f"Verify failed after deploying {run.run_id} ({note}). "
                                                 f"Rolled back to {cur.run_id}: {back.status}.")
                    return self._get(dep_id)
                self._finish(dep_id, VERIFY_FAILED, note)
                if self.notifier:
                    self.notifier.deployment("deploy_failed", env.name, run.run_id, f"verify failed: {note}")
                return self._get(dep_id)

        self._finish(dep_id, SUCCEEDED)
        (log_dir / "deployment.json").write_text(json.dumps({
            "id": dep_id, "env": env.name, "run_id": run.run_id, "commit": run.commit, "kind": kind, "by": by,
            "previous_run_id": cur.run_id if cur else "", "finished": _now()}, indent=2), encoding="utf-8")
        self.log(f"✓ {env.name} now runs {run.run_id}")
        if self.notifier:
            self.notifier.deployment("deployed", env.name, run.run_id,
                                     f"{kind}: {env.name} now runs {run.run_id} (commit {run.commit[:8]}), by {by}.")
        return self._get(dep_id)

    def _step(self, step: str, command: str, run: RunResult, env: Environment, dep_id: int, log_dir: Path) -> JobResult:
        job = Job(id=f"{step}-{env.name}", gate="deploy", category="ops", run=command, timeout=env.timeout,
                  secrets=list(env.secrets), shell=env.shell, cache=False,  # nosec B604 - `shell` is a config string
                  env={**{k: str(v) for k, v in env.env.items()},
                       "GATEWRIGHT_ARTIFACT": run.artifact_dir, "GATEWRIGHT_DEPLOY_RUN_ID": run.run_id,
                       "GATEWRIGHT_COMMIT": run.commit, "GATEWRIGHT_ENV": env.name,
                       "GATEWRIGHT_DEPLOY_ID": str(dep_id)})
        res = execute(job, root=self.root, run_id=f"deploy-{dep_id}", out_dir=str(log_dir / "out"),
                      log_file=str(log_dir / f"{step}.log"), secrets=SecretStore(self.root), handle=JobHandle(job),
                      on_log=lambda jid, line: self.log(f"    {jid}│ {line}"))
        return res

    def _write_release_notes(self, run: RunResult, env: Environment, cur: Deployment | None, log_dir: Path) -> None:
        from .releasenotes import generate
        try:
            notes = generate(self.root, cur.commit_sha if cur and cur.commit_sha else None, run.commit, run,
                             title=f"Release {run.run_id} → {env.name}")
            (log_dir / "release-notes.md").write_text(notes, encoding="utf-8")
        except ValueError as e:
            self.log(f"! release notes skipped: {e}")

    def _get(self, dep_id: int) -> Deployment:
        return next(d for d in self.deployments(None, 1000) if d.id == dep_id)


def _first_error(res: JobResult) -> str:
    e = next(iter(res.active_errors), None)
    if e:
        return e.human[:200]
    return f"{res.status} (exit {res.exit_code})" if res.status == ERROR else res.status


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # no user database (some containers)
        return "unknown"


def _ask(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False
