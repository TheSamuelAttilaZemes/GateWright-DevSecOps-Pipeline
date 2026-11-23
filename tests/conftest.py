# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


def _job_python(exe: str) -> str:
    """The interpreter path as tests write it into job commands.

    Job commands are embedded in YAML (where `\\U` in a double-quoted string is an escape)
    and run through Git Bash on Windows (which strips unquoted backslashes and splits on
    spaces). On Windows use forward slashes and, if the path has spaces, its 8.3 short form.
    """
    if os.name != "nt":
        return exe
    if " " in exe:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetShortPathNameW(exe, buf, len(buf)) and " " not in buf.value:
            exe = buf.value
    return exe.replace("\\", "/")


PY = _job_python(sys.executable)

# Many tests write job commands in POSIX shell syntax (sleep, cat, test -f, $VAR, `;`).
# On Windows Gatewright runs those through Git Bash when it is installed; without it they
# would fail for reasons that have nothing to do with Gatewright, so they are skipped.
from gatewright.runner import _windows_bash  # noqa: E402

HAS_POSIX_SHELL = os.name != "nt" or bool(_windows_bash())
needs_posix_shell = pytest.mark.skipif(
    not HAS_POSIX_SHELL, reason="needs a POSIX shell for job commands: install Git for Windows (Git Bash)")
posix_only = pytest.mark.skipif(os.name == "nt", reason="tests Linux/macOS-only behaviour")


def make_project(root: Path, pipeline: str, jobs: str, files: dict[str, str] | None = None,
                 git: bool = False) -> Path:
    (root / ".gatewright" / "jobs").mkdir(parents=True, exist_ok=True)
    # Gatewright reads its files as UTF-8; write them that way, not in the Windows code page
    (root / ".gatewright" / "pipeline.yml").write_text(textwrap.dedent(pipeline), encoding="utf-8")
    (root / ".gatewright" / "jobs" / "jobs.yml").write_text(textwrap.dedent(jobs).replace("{PY}", PY),
                                                              encoding="utf-8")
    for name, content in (files or {}).items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(content), encoding="utf-8")
    if git:
        (root / ".gitignore").write_text(".gatewright/runs/\n.gatewright/cache/\n.gatewright/gatewright.db\n")
        for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                    ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
            subprocess.run(cmd, cwd=root, check=True)
    return root


@pytest.fixture(autouse=True, scope="session")
def _git_stays_in_tmp(tmp_path_factory):
    """Never let git walk up out of the test folders into a repository that encloses them
    (e.g. a home folder under version control): tests would then read, or install hooks
    into, that repository instead of failing as "not a git repository"."""
    base = str(tmp_path_factory.getbasetemp())
    old = os.environ.get("GIT_CEILING_DIRECTORIES")
    os.environ["GIT_CEILING_DIRECTORIES"] = base
    yield
    if old is None:
        os.environ.pop("GIT_CEILING_DIRECTORIES", None)
    else:
        os.environ["GIT_CEILING_DIRECTORIES"] = old


@pytest.fixture(autouse=True)
def _no_retry_waits(monkeypatch):
    """Failed notifications are retried; tests don't need to wait for that."""
    from gatewright import notify
    monkeypatch.setattr(notify, "RETRY_WAITS", [0, 0])


@pytest.fixture
def project(tmp_path):
    def _make(pipeline, jobs, files=None, git=False):
        return make_project(tmp_path, pipeline, jobs, files, git)
    return _make


BASIC_PIPELINE = """\
version: 1
name: t
profile: server
gates:
  - id: commit
    mode: enforced
  - id: test
    mode: enforced
"""
