# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""git pre-push hook: install/uninstall, backups, and a real push blocked by a failing gate."""

import shutil
import subprocess

import pytest

from gatewright import hooks

from .conftest import BASIC_PIPELINE, posix_only

from .conftest import PY  # noqa: E402


def _repo(project):
    return project(BASIC_PIPELINE, f"""\
        jobs:
          - {{id: check, gate: commit, run: '{PY} check.py', cache: false}}
    """, files={"check.py": "import os, sys\nsys.exit(1 if os.path.exists('BROKEN') else 0)\n"}, git=True)


def test_install_status_uninstall_with_backup(project):
    root = _repo(project)
    hooks_dir = hooks.hooks_dir(str(root))
    assert hooks.status(str(root)) == "not installed"
    (hooks_dir / "pre-push").write_text("#!/bin/sh\necho mine\n")
    with pytest.raises(FileExistsError, match="--force"):
        hooks.install(str(root), ["commit"])
    hooks.install(str(root), ["commit"], force=True)
    assert hooks.status(str(root)) == "installed"
    assert (hooks_dir / "pre-push.before-gatewright").read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"
    assert "gatewright run -g commit" in (hooks_dir / "pre-push").read_text(encoding="utf-8")
    assert hooks.uninstall(str(root)) == "removed; previous pre-push hook restored"
    assert (hooks_dir / "pre-push").read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"
    assert hooks.uninstall(str(root)) == "no gatewright hook installed"


def test_not_a_git_repo(tmp_path):
    with pytest.raises(ValueError, match="not a git repository"):
        hooks.install(str(tmp_path), ["commit"])


@posix_only
@pytest.mark.skipif(not shutil.which("gatewright"), reason="the gatewright command is not on PATH")
def test_real_push_is_blocked_by_failing_gate(project, tmp_path):
    root = _repo(project)
    bare = tmp_path.parent / (tmp_path.name + "-remote.git")
    subprocess.run(["git", "clone", "-q", "--bare", str(root), str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=root, check=True)
    hooks.install(str(root), ["commit"])

    def push():
        return subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=root, capture_output=True, text=True)

    (root / "BROKEN").write_text("x")
    assert push().returncode != 0                      # the commit gate fails -> push refused
    (root / "BROKEN").unlink()
    assert push().returncode == 0, push().stderr       # fixed -> push goes through
