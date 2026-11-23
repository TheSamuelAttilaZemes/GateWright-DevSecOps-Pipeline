# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""A lock between processes, e.g. two `gatewright promote` commands for one environment.

The operating system holds the lock, not the file's existence: it is released when the
process ends, also after a crash, so there is never a stale lock to clean up.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path


class Locked(Exception):
    """Another process (or thread) holds the lock."""


def _acquire(fh) -> None:
    if sys.platform == "win32":  # pragma: no cover - Windows only
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(fh) -> None:
    if sys.platform == "win32":  # pragma: no cover - Windows only
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(path: Path):
    """Hold `path` as a lock; raises Locked at once if someone else holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as fh:
        try:
            _acquire(fh)
        except OSError:
            raise Locked(str(path)) from None
        try:
            yield
        finally:
            _release(fh)
