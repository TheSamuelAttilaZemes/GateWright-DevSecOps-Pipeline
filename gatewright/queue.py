# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The approval queue (v0.5): commits waiting to be run, stored in .gatewright/gatewright.db.

Life of a queue item:

    pending ──approve──▶ approved ──watcher picks it up──▶ running ──▶ done
       │                                                       │
       ├──reject──▶ rejected                                   └─(crash)─▶ error
       └──newer commit on the same branch/PR──▶ superseded

Every item is pinned to one commit SHA. Approving item 7 runs exactly that
commit, even if the branch has moved on since (a later push becomes a new item).
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .vcs.base import VCSEvent

PENDING, APPROVED, RUNNING, DONE, REJECTED, SUPERSEDED, ERROR = (
    "pending", "approved", "running", "done", "rejected", "superseded", "error")
OPEN_STATES = (PENDING, APPROVED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, kind TEXT, sha TEXT, ref TEXT, pr INTEGER,
    author TEXT, from_fork INTEGER, title TEXT, url TEXT, trusted INTEGER, state TEXT,
    created TEXT, decided_by TEXT, decided_at TEXT, run_id TEXT, result TEXT, note TEXT
);
CREATE INDEX IF NOT EXISTS queue_key ON queue(key, sha);
CREATE TABLE IF NOT EXISTS vcs_state (name TEXT PRIMARY KEY, value TEXT);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class QueueItem:
    """One commit waiting for (or done with) a run."""
    id: int
    key: str
    kind: str
    sha: str
    ref: str
    pr: int | None
    author: str
    from_fork: bool
    title: str
    url: str
    trusted: bool
    state: str
    created: str
    decided_by: str = ""
    decided_at: str = ""
    run_id: str = ""
    result: str = ""
    note: str = ""

    @property
    def event(self) -> VCSEvent:
        return VCSEvent(self.kind, self.sha, self.ref, self.author, self.pr, self.from_fork, self.title, self.url)

    @property
    def label(self) -> str:
        what = f"PR #{self.pr}" if self.kind == "pull_request" else f"branch {self.ref}"
        return f"{what} @ {self.sha[:8]}" + (f" by {self.author}" if self.author else "")


class Queue:
    """SQLite-backed approval queue and small key/value store for the watcher."""

    def __init__(self, root: str):
        self.path = Path(root) / ".gatewright" / "gatewright.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conn()) as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=15, isolation_level=None)  # autocommit; explicit BEGIN below
        c.row_factory = sqlite3.Row
        return c

    @staticmethod
    def _item(r: sqlite3.Row) -> QueueItem:
        d = dict(r)
        d["from_fork"], d["trusted"] = bool(d["from_fork"]), bool(d["trusted"])
        for k in ("decided_by", "decided_at", "run_id", "result", "note"):
            d[k] = d[k] or ""
        return QueueItem(**d)

    def add(self, ev: VCSEvent, trusted: bool, auto_approve: bool) -> tuple[QueueItem | None, list[QueueItem]]:
        """Queue an event. Returns (new item or None if this commit is already known,
        older open items of the same branch/PR that are now superseded)."""
        with closing(self._conn()) as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                if c.execute("SELECT 1 FROM queue WHERE key=? AND sha=?", (ev.key, ev.sha)).fetchone():
                    c.execute("COMMIT")
                    return None, []
                older = [self._item(r) for r in c.execute(
                    "SELECT * FROM queue WHERE key=? AND state IN (?,?)", (ev.key, *OPEN_STATES))]
                for o in older:
                    c.execute("UPDATE queue SET state=?, note=? WHERE id=?",
                              (SUPERSEDED, f"superseded by {ev.sha[:8]}", o.id))
                state = APPROVED if auto_approve else PENDING
                cur = c.execute(
                    "INSERT INTO queue (key, kind, sha, ref, pr, author, from_fork, title, url, trusted, state, "
                    "created, decided_by, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ev.key, ev.kind, ev.sha, ev.ref, ev.pr, ev.author, int(ev.from_fork), ev.title, ev.url,
                     int(trusted), state, _now(), "auto" if auto_approve else "", _now() if auto_approve else ""))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
            return self.get(cur.lastrowid), older

    def get(self, item_id: int) -> QueueItem | None:
        with closing(self._conn()) as c:
            r = c.execute("SELECT * FROM queue WHERE id=?", (item_id,)).fetchone()
        return self._item(r) if r else None

    def list(self, states: tuple[str, ...] | None = None, limit: int = 50) -> list[QueueItem]:
        with closing(self._conn()) as c:
            if states:
                rows = c.execute(f"SELECT * FROM queue WHERE state IN ({','.join('?' * len(states))}) "  # nosec B608
                                 "ORDER BY id DESC LIMIT ?", (*states, limit))
            else:
                rows = c.execute("SELECT * FROM queue ORDER BY id DESC LIMIT ?", (limit,))
            return [self._item(r) for r in rows]

    def _decide(self, item_id: int, to: str, by: str, note: str = "") -> QueueItem:
        with closing(self._conn()) as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT state FROM queue WHERE id=?", (item_id,)).fetchone()
            if not r:
                c.execute("ROLLBACK")
                raise ValueError(f"No queue item #{item_id}.")
            if r["state"] != PENDING:
                c.execute("ROLLBACK")
                raise ValueError(f"Queue item #{item_id} is {r['state']}, only pending items can be approved or rejected.")
            c.execute("UPDATE queue SET state=?, decided_by=?, decided_at=?, note=? WHERE id=?",
                      (to, by, _now(), note, item_id))
            c.execute("COMMIT")
        return self.get(item_id)

    def approve(self, item_id: int, by: str) -> QueueItem:
        """Approve a pending item: the watcher will run exactly its commit."""
        return self._decide(item_id, APPROVED, by)

    def reject(self, item_id: int, by: str, reason: str = "") -> QueueItem:
        """Reject a pending item: it will never run."""
        return self._decide(item_id, REJECTED, by, reason)

    def last_id(self) -> int:
        with closing(self._conn()) as c:
            return c.execute("SELECT COALESCE(MAX(id), 0) FROM queue").fetchone()[0]

    def close_missing(self, open_prs: set[int], up_to: int) -> list[QueueItem]:
        """Retire pending items (id <= up_to) of pull requests that are no longer open."""
        with closing(self._conn()) as c:
            c.execute("BEGIN IMMEDIATE")
            gone = [self._item(r) for r in c.execute(
                "SELECT * FROM queue WHERE kind='pull_request' AND state=? AND id<=?", (PENDING, up_to))
                if r["pr"] not in open_prs]
            for i in gone:
                c.execute("UPDATE queue SET state=?, note=? WHERE id=?",
                          (SUPERSEDED, "the pull request was closed or merged", i.id))
            c.execute("COMMIT")
        return gone

    def claim_next(self) -> QueueItem | None:
        """Atomically take the oldest approved item and mark it running (safe with several watchers)."""
        with closing(self._conn()) as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT * FROM queue WHERE state=? ORDER BY id LIMIT 1", (APPROVED,)).fetchone()
            if not r:
                c.execute("COMMIT")
                return None
            c.execute("UPDATE queue SET state=? WHERE id=?", (RUNNING, r["id"]))
            c.execute("COMMIT")
        return self.get(r["id"])

    def finish(self, item_id: int, state: str, run_id: str = "", result: str = "", note: str = "") -> None:
        with closing(self._conn()) as c:
            c.execute("UPDATE queue SET state=?, run_id=?, result=?, note=? WHERE id=?",
                      (state, run_id, result, note, item_id))

    # --- small persistent state for the watcher (ETags, heartbeat) ----------------
    def get_state(self, name: str, default=None):
        with closing(self._conn()) as c:
            r = c.execute("SELECT value FROM vcs_state WHERE name=?", (name,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set_state(self, name: str, value) -> None:
        with closing(self._conn()) as c:
            c.execute("INSERT INTO vcs_state (name, value) VALUES (?, ?) "
                      "ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, json.dumps(value)))

    def heartbeat(self) -> None:
        self.set_state("watcher_heartbeat", time.time())

    def watcher_alive(self, within: float = 180) -> bool:
        """True if a watcher process reported in recently."""
        hb = self.get_state("watcher_heartbeat")
        return bool(hb) and time.time() - hb < within
