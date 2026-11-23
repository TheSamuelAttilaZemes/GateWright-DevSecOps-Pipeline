# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The artifact bundle: everything a run produced, in one verifiable package.

.gatewright/runs/<run_id>/
    manifest.json        what ran, on what commit, with which tools, and the results
    logs/<job>.log       full (secret-masked) output of each job
    out/<job>/...        files the job wrote to $GATEWRIGHT_OUT, plus declared `outputs`
    report.md, gate-*.md, report.html, report.pdf, results.sarif, junit.xml
    checksums.sha256     SHA-256 of every file above (tamper evidence)
.gatewright/runs/gatewright-run-<run_id>.tar.gz   the whole folder, ready to store or ship
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import shutil
import tarfile
from pathlib import Path

from . import __version__
from .models import RunResult
from .winsupport import long_path


# Files that are created after checksums.sha256 and therefore not listed in it:
# the checksum file itself and its signature (the signature covers the checksums).
UNLISTED = {"checksums.sha256", "checksums.sha256.sig", "signature.json"}


def sha256_file(path: Path) -> str:
    """SHA-256 hex digest of a file, read in chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_outputs(root: str, run_dir: Path, job_id: str, patterns: list[str]) -> list[str]:
    """Copy files matching a job's `outputs:` globs into the run folder."""
    copied = []
    run_dir = long_path(run_dir)
    dest = run_dir / "out" / job_id
    root_path = long_path(Path(root).resolve())
    for pat in patterns:
        # normpath: Windows' extended paths (winsupport.extended) know neither `/` nor `..`
        for src in glob.glob(os.path.normpath(pat), root_dir=root_path, recursive=True):
            sp = long_path(os.path.join(root_path, src)).resolve()
            try:
                rel = sp.relative_to(root_path)
            except ValueError:  # `../x` or an absolute path: never copy files from outside the project
                continue
            if not sp.is_file() or rel.parts[:1] == (".gatewright",):
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sp, target)
            copied.append(target.relative_to(run_dir).as_posix())  # `/` on every OS, like the rest of the manifest
    return copied


def write_manifest(run: RunResult, run_dir: Path) -> Path:
    """Write manifest.json: the full RunResult plus Gatewright's version."""
    data = {"gatewright_version": __version__, "schema": 1, **run.to_dict()}
    p = run_dir / "manifest.json"
    p.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return p


def write_checksums(run_dir: Path) -> Path:
    """Write checksums.sha256 for every file in the run folder (sha256sum format)."""
    lines = []
    run_dir = long_path(run_dir)
    for p in sorted(run_dir.rglob("*")):
        if p.is_file() and p.name != "checksums.sha256":
            lines.append(f"{sha256_file(p)}  {p.relative_to(run_dir).as_posix()}")
    out = run_dir / "checksums.sha256"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def verify(run_dir: Path) -> list[str]:
    """Return a list of problems (empty = bundle intact)."""
    run_dir = long_path(run_dir)
    cs = run_dir / "checksums.sha256"
    if not cs.exists():
        return ["checksums.sha256 is missing"]
    problems = []
    listed = set()
    for n, line in enumerate(cs.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        digest, sep, rel = line.partition("  ")
        if not sep or len(digest) != 64 or not rel:
            problems.append(f"checksums.sha256 line {n} is malformed")
            continue
        if rel.startswith("/") or ".." in Path(rel).parts:
            problems.append(f"checksums.sha256 line {n} points outside the artifact: {rel}")
            continue
        listed.add(rel)
        p = run_dir / rel
        if not p.is_file():
            problems.append(f"missing: {rel}")
        elif sha256_file(p) != digest:
            problems.append(f"modified: {rel}")
    for p in run_dir.rglob("*"):
        if p.is_file() and p.name not in UNLISTED and p.relative_to(run_dir).as_posix() not in listed:
            problems.append(f"unexpected file: {p.relative_to(run_dir).as_posix()}")
    return problems


def pack(run_dir: Path) -> Path:
    """Create gatewright-run-<id>.tar.gz next to the run folder."""
    archive = run_dir.parent / f"gatewright-run-{run_dir.name}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(long_path(run_dir), arcname=run_dir.name)
    return archive
