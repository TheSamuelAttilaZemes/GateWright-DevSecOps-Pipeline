# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Job result caching: skip a job when its definition, tool version and input
files are identical to a previous passing run.

The cache key is a SHA-256 over: the job definition (every field), the tool's
`--version` output, and the content of every project file matching the job's
`inputs:` globs (default: all files, minus IGNORED_DIRS). Only PASSED/WARN
results are stored (.gatewright/cache/<job>.json); failures always re-run.
Container jobs (`image:`) are never cached, because the image can change
without its name changing.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path

from .models import Job, JobResult

IGNORED_DIRS = {".git", ".gatewright", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache",
                ".pytest_cache", ".ruff_cache", "dist", "build", ".tox"}


def project_files(root: str) -> list[str]:
    """All project files (sorted, '/'-separated), skipping IGNORED_DIRS."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS and not d.endswith(".egg-info"))
        for f in sorted(filenames):
            out.append(os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/"))
    return out


class Cache:
    """Per-project job result cache in .gatewright/cache/."""
    def __init__(self, root: str, cache_dir: Path | None = None):
        self.root = root
        self.dir = Path(cache_dir) if cache_dir else Path(root) / ".gatewright" / "cache"
        self._files: list[str] | None = None
        self._hashes: dict[str, str] = {}

    def _all_files(self) -> list[str]:
        if self._files is None:
            self._files = project_files(self.root)
        return self._files

    def _file_hash(self, rel: str) -> str:
        if rel not in self._hashes:
            h = hashlib.sha256()
            try:
                with open(os.path.join(self.root, rel), "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 16), b""):
                        h.update(chunk)
            except OSError:
                h.update(b"<unreadable>")
            self._hashes[rel] = h.hexdigest()
        return self._hashes[rel]

    def key(self, job: Job, tool_version: str, extra: str = "") -> str:
        """Cache key for a job (see module docstring for what goes in)."""
        patterns = job.inputs or ["**"]
        files = [f for f in self._all_files()
                 if any(fnmatch.fnmatch(f, p) or (p.startswith("**/") and fnmatch.fnmatch(f, p[3:]))
                        for p in patterns)]
        h = hashlib.sha256()
        h.update(job.definition_hash().encode())
        h.update(tool_version.encode())
        h.update(extra.encode())
        for f in files:
            h.update(f.encode())
            h.update(self._file_hash(f).encode())
        return h.hexdigest()

    def get(self, job: Job, key: str) -> JobResult | None:
        """Cached result if the key matches, else None."""
        path = self.dir / f"{job.id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("key") != key:
                return None
            return JobResult.from_dict(data["result"])
        except (OSError, ValueError, KeyError, TypeError):  # corrupt/foreign cache file = cache miss
            return None

    def put(self, job: Job, key: str, result: JobResult) -> None:
        """Store a result (only passed/warn results are cached)."""
        if result.status not in ("passed", "warn"):
            return  # never cache skipped/failed results
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{job.id}.json").write_text(json.dumps({"key": key, "result": result.to_dict()}), encoding="utf-8")

    def clear(self) -> int:
        """Delete all cached results; returns how many were removed."""
        n = 0
        if self.dir.exists():
            for f in self.dir.glob("*.json"):
                f.unlink()
                n += 1
        return n
