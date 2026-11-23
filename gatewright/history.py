# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Run history in SQLite (.gatewright/gatewright.db): list runs, load them back, and diff
findings between runs.

Tables:
  runs          one row per run; `data` holds the full RunResult as JSON
  issues        every JobError of every run (for queries and trends)
  gate_results  gate status per run and commit (used by gate `requires`)

Order is by SQLite rowid (insertion order), never by timestamp: two runs can
start in the same second. If you change the schema, add a migration step in
`History.__init__` - users keep their database across upgrades.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .models import JobError, RunResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, pipeline TEXT, started TEXT, finished TEXT, status TEXT,
    commit_sha TEXT, branch TEXT, dirty INTEGER, profile TEXT, gates TEXT,
    duration REAL, artifact_dir TEXT, resumed_from TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS issues (
    run_id TEXT, job_id TEXT, gate TEXT, fingerprint TEXT, severity TEXT, category TEXT,
    file TEXT, line INTEGER, rule TEXT, human TEXT, suppressed INTEGER
);
CREATE INDEX IF NOT EXISTS issues_run ON issues(run_id);
CREATE TABLE IF NOT EXISTS gate_results (
    run_id TEXT, gate TEXT, status TEXT, commit_sha TEXT
);
"""


class History:
    """SQLite run history for one project."""
    def __init__(self, root: str):
        self.path = Path(root) / ".gatewright" / "gatewright.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conn()) as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def save(self, run: RunResult) -> None:
        """Insert or replace a run (and its issues and gate results)."""
        with closing(self._conn()) as c, c:
            c.execute("DELETE FROM runs WHERE id=?", (run.run_id,))
            c.execute("DELETE FROM issues WHERE run_id=?", (run.run_id,))
            c.execute("DELETE FROM gate_results WHERE run_id=?", (run.run_id,))
            c.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                run.run_id, run.pipeline, run.started, run.finished, run.status, run.commit, run.branch,
                int(run.dirty), run.profile, ",".join(g.gate for g in run.gates), run.duration,
                run.artifact_dir, run.resumed_from, json.dumps(run.to_dict())))
            for g in run.gates:
                c.execute("INSERT INTO gate_results VALUES (?,?,?,?)", (run.run_id, g.gate, g.status, run.commit))
                for j in g.jobs:
                    for e in j.errors:
                        c.execute("INSERT INTO issues VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                            run.run_id, j.job_id, g.gate, e.fingerprint, e.severity, e.category,
                            e.file, e.line, e.rule, e.human, int(e.suppressed)))

    def list(self, limit: int = 20) -> list[sqlite3.Row]:
        """Most recent runs first (summary columns only)."""
        with closing(self._conn()) as c:
            return c.execute("SELECT id, started, status, commit_sha, branch, gates, duration, resumed_from "
                             "FROM runs ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()

    def load(self, run_id: str | None = None) -> RunResult | None:
        """Load a full RunResult by id, id prefix, 'last'/'latest' or 'previous'."""
        with closing(self._conn()) as c:
            if run_id in (None, "last", "latest"):
                row = c.execute("SELECT data FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
            elif run_id == "previous":
                row = c.execute("SELECT data FROM runs ORDER BY rowid DESC LIMIT 1 OFFSET 1").fetchone()
            else:
                row = c.execute("SELECT data FROM runs WHERE id=? OR id LIKE ? ORDER BY rowid DESC LIMIT 1",
                                (run_id, f"{run_id}%")).fetchone()
        return RunResult.from_dict(json.loads(row["data"])) if row else None

    def gate_passed(self, gate: str, commit: str) -> str | None:
        """Most recent run id where `gate` passed for `commit`."""
        with closing(self._conn()) as c:
            row = c.execute(
                "SELECT g.run_id FROM gate_results g JOIN runs r ON r.id=g.run_id "
                "WHERE g.gate=? AND g.commit_sha=? AND g.status IN ('passed','warn') "
                "ORDER BY r.rowid DESC LIMIT 1", (gate, commit)).fetchone()
        return row["run_id"] if row else None


def diff(old: RunResult, new: RunResult) -> dict[str, list[JobError]]:
    """Compare active (unsuppressed) issues by fingerprint."""
    def active(run):
        return {e.fingerprint: e for e in run.all_errors() if not e.suppressed and not e.is_infra}
    a, b = active(old), active(new)
    return {
        "new": [b[k] for k in b if k not in a],
        "fixed": [a[k] for k in a if k not in b],
        "unchanged": [b[k] for k in b if k in a],
    }
