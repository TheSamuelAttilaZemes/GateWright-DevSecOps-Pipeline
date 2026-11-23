# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Git pre-push hook (v0.5): run a gate (default: commit) before every `git push`.

`gatewright hook install` writes .git/hooks/pre-push (respecting core.hooksPath and
worktrees). An existing hook that isn't ours is never overwritten silently: use
--force, and it is backed up to pre-push.before-gatewright and restored on uninstall.
Skip the hook once with `git push --no-verify`.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess  # nosec B404 - fixed git argv
from pathlib import Path

MARK = "# installed by gatewright"
BACKUP_SUFFIX = ".before-gatewright"


def hooks_dir(root: str) -> Path:
    """The repository's hooks folder (honours core.hooksPath and worktrees)."""
    p = subprocess.run(["git", "rev-parse", "--git-path", "hooks"], cwd=root, capture_output=True,  # nosec B603 B607
                       text=True, check=False)
    if p.returncode != 0:
        raise ValueError(f"{root} is not a git repository.")
    d = Path(p.stdout.strip())
    return d if d.is_absolute() else Path(root) / d


def script(gates: list[str]) -> str:
    g = ",".join(gates)
    return f"""#!/bin/sh
{MARK} - remove with: gatewright hook uninstall
# Runs the gate(s) {g} before every push. Skip once with: git push --no-verify
if ! command -v gatewright >/dev/null 2>&1; then
    echo "gatewright not found on PATH; skipping pre-push checks" >&2
    exit 0
fi
echo "gatewright: running gate(s) {g} before push (skip with --no-verify)"
exec gatewright run -g {g} -q
"""


def status(root: str) -> str:
    hook = hooks_dir(root) / "pre-push"
    if not hook.exists():
        return "not installed"
    return "installed" if MARK in hook.read_text(encoding="utf-8", errors="replace") else "another pre-push hook is installed"


GATE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def install(root: str, gates: list[str], force: bool = False) -> Path:
    # the names end up in a shell script: only plain gate ids, never shell syntax
    bad = [g for g in gates if not GATE_ID.fullmatch(g)]
    if bad or not gates:
        raise ValueError(f"Invalid gate name(s) for the hook: {', '.join(bad) or '(none)'}. "
                         "Use gate ids from pipeline.yml (letters, digits, - _ .).")
    d = hooks_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    hook = d / "pre-push"
    if hook.exists() and MARK not in hook.read_text(encoding="utf-8", errors="replace"):
        if not force:
            raise FileExistsError(f"{hook} already exists and was not installed by gatewright. "
                                  "Use --force to replace it (it is backed up and restored on uninstall).")
        hook.replace(hook.with_name(hook.name + BACKUP_SUFFIX))
    hook.write_text(script(gates), encoding="utf-8", newline="\n")  # LF: git runs hooks with sh, also on Windows
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return hook


def uninstall(root: str) -> str:
    d = hooks_dir(root)
    hook, backup = d / "pre-push", d / ("pre-push" + BACKUP_SUFFIX)
    if not hook.exists() or MARK not in hook.read_text(encoding="utf-8", errors="replace"):
        return "no gatewright hook installed"
    hook.unlink()
    if backup.exists():
        os.replace(backup, hook)
        return "removed; previous pre-push hook restored"
    return "removed"
