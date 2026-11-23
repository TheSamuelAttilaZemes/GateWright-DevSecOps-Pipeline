# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Delete old run folders and archives (`gatewright clean`, `keep_runs:` in pipeline.yml).

Never deleted: the newest runs, runs that were ever deployed (rollbacks need them), and
folders that are not in the history (a run in progress). The history itself is kept, so
`gatewright history` and gate `requires` still work.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .history import History
from .winsupport import long_path

RUN_ID = re.compile(r"[A-Za-z0-9-]+")


def deployed_runs(root: str) -> set[str]:
    """Every run that was ever deployed, or was being deployed, to any environment."""
    with closing(sqlite3.connect(Path(root) / ".gatewright" / "gatewright.db", timeout=15)) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='deployments'").fetchone():
            return set()
        return {v for row in c.execute("SELECT run_id, previous_run_id FROM deployments") for v in row if v}


def _paths(runs: Path, run_id: str) -> list[Path]:
    return [p for p in (runs / run_id, runs / f"gatewright-run-{run_id}.tar.gz") if p.exists() and not p.is_symlink()]


def old_runs(root: str, keep: int, days: int = 0) -> list[str]:
    """Ids of the runs whose files may be deleted: not among the newest `keep`, older than `days`."""
    runs = Path(root) / ".gatewright" / "runs"
    protected = deployed_runs(root)
    limit = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    return [r["id"] for r in History(root).list(1_000_000)[max(1, keep):]
            if r["id"] not in protected and RUN_ID.fullmatch(r["id"]) and _paths(runs, r["id"])
            and (not days or r["started"] < limit)]


def size_of(root: str, run_id: str) -> int:
    """Bytes used by a run's folder and archive."""
    total = 0
    for p in _paths(Path(root) / ".gatewright" / "runs", run_id):
        files = long_path(p).rglob("*") if p.is_dir() else [p]
        total += sum(f.stat().st_size for f in files if f.is_file() and not f.is_symlink())
    return total


def delete(root: str, run_id: str) -> bool:
    """Delete a run's folder and archive. False if something could not be deleted."""
    for p in _paths(Path(root) / ".gatewright" / "runs", run_id):
        try:
            if p.is_dir():
                shutil.rmtree(long_path(p))
            else:
                p.unlink()
        except OSError:
            return False
    return True
