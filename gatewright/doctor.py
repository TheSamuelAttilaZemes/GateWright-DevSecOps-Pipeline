# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""`gatewright doctor`: is this machine ready to run the pipeline?

Checks the OS/distribution, Python, git, the Python jobs will use, the shell,
Docker (required only if a job uses `image:`), PDF support, and every tool the
pipeline's jobs call. Each failed check carries an install command for *this*
OS - see platform_info.install_hint and INSTALL_HINTS below (fallback links).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess  # nosec B404
import sys
from dataclasses import dataclass

from .models import Pipeline
from .runner import tool_available, tool_version
from .winsupport import long_paths_enabled

INSTALL_HINTS = {
    "git": "https://git-scm.com/downloads (Debian/Ubuntu: sudo apt install git)",
    "docker": "https://docs.docker.com/engine/install/ (or Podman with `alias docker=podman`)",
    "ruff": "pip install ruff",
    "bandit": "pip install bandit",
    "pytest": "pip install pytest",
    "mypy": "pip install mypy",
    "coverage": "pip install coverage",
    "pip-audit": "pip install pip-audit",
    "semgrep": "pip install semgrep",
    "trivy": "https://aquasecurity.github.io/trivy/latest/getting-started/installation/",
    "gitleaks": "https://github.com/gitleaks/gitleaks#installing (or `brew install gitleaks`)",
    "hadolint": "https://github.com/hadolint/hadolint#install",
    "checkov": "pip install checkov",
    "yamllint": "pip install yamllint",
    "npm": "https://nodejs.org/",
    "node": "https://nodejs.org/",
    "eslint": "npm install --save-dev eslint",
    "syft": "https://github.com/anchore/syft#installation",
}


@dataclass
class Check:
    """One line of `gatewright doctor` output. Optional checks only warn."""
    name: str
    ok: bool
    detail: str
    hint: str = ""
    required: bool = True


def weasyprint_problem() -> str | None:
    """Import WeasyPrint quietly (it prints help URLs to stderr when Pango is missing)."""
    import contextlib
    import io
    try:
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            import weasyprint  # noqa: F401
    except Exception as e:  # OSError for missing pango/gobject
        msg = str(e).split(":")[0]
        return msg[:80]
    return None


def _py_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def run_doctor(pipeline: Pipeline | None) -> list[Check]:
    """Run every check for this machine and (optionally) this pipeline."""
    from .platform_info import detect as detect_os, install_hint
    osi = detect_os()

    def hint(tool: str) -> str:
        return install_hint(tool, osi, INSTALL_HINTS.get(tool, f"install `{tool}` and make sure it is on PATH"))

    detail = f"{osi.name} · family {osi.family} · package manager {osi.package_manager or 'unknown'}"
    if osi.wsl:
        detail += " · WSL"
    checks = [Check("os", osi.family != "unknown", detail,
                    "Unknown distribution: Gatewright still works, but install hints will be generic", required=False),
              Check("python", sys.version_info >= (3, 10), sys.version.split()[0],
                    "Gatewright needs Python 3.10 or newer (re-run install.sh, it installs a newer Python if needed)")]
    git = shutil.which("git")
    checks.append(Check("git", bool(git), tool_version("git") if git else "not found (commit tracking and gate `requires` disabled)",
                        hint("git"), required=False))
    from .runner import python_for_jobs
    root = pipeline.root if pipeline else None
    py, src = python_for_jobs(root)
    checks.append(Check("python for jobs", bool(py), f"{py or 'not found'} ({src})",
                        "Create a project venv: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest"))

    from .runner import shell_command
    shells = {j.shell for j in pipeline.jobs if not j.image} if pipeline else {"auto"}
    for sh in sorted(shells or {"auto"}):
        _, _, resolved = shell_command("true", sh)
        if os.name == "nt" and resolved == "cmd" and sh == "auto":
            checks.append(Check("shell", False, "cmd.exe (Git Bash not found)",
                                "Install Git for Windows (includes Git Bash) so jobs written for sh/bash run unchanged, "
                                "or set `shell: powershell`/`cmd` and write the commands for that shell", required=False))
        else:
            checks.append(Check("shell", True, f"{sh} -> {resolved}"))
    needs_docker = bool(pipeline and any(j.image for j in pipeline.jobs))
    long_paths = long_paths_enabled()
    if long_paths is not None:
        checks.append(Check("long paths", long_paths, "enabled" if long_paths else
                            "disabled (Gatewright handles paths over 260 characters, but your tools may not)",
                            "In an administrator PowerShell: Set-ItemProperty "
                            r"HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem LongPathsEnabled 1",
                            required=False))
    docker = shutil.which("docker")
    if docker:
        try:
            ok = subprocess.run([docker, "info"], capture_output=True, timeout=10, check=False).returncode == 0  # nosec
        except (OSError, subprocess.SubprocessError):
            ok = False
        checks.append(Check("docker", ok, "daemon running" if ok else "installed, but the daemon is not reachable",
                            "Start Docker: `sudo systemctl start docker` (Linux) or open Docker Desktop",
                            required=needs_docker))
    else:
        checks.append(Check("docker", False, "not found", hint("docker"), required=needs_docker))

    pdf = _py_module("weasyprint")
    detail = "installed" if pdf else "not installed (PDF reports disabled)"
    if pdf:
        err = weasyprint_problem()
        if err:
            pdf, detail = False, f"installed, but system library missing ({err})"
    checks.append(Check("weasyprint (PDF)", pdf, detail,
                        f"pip install weasyprint; system library: {hint('pango') or 'Pango'}",
                        required=False))

    if pipeline and pipeline.signing:
        from . import signing
        cfg = signing.SigningConfig.from_dict(pipeline.signing)
        ver = signing.openssh_version()
        has = shutil.which("ssh-keygen") is not None
        ok = has and (ver is None or ver >= (8, 2))
        detail = f"OpenSSH {ver[0]}.{ver[1]}" if ver else ("found" if has else "not found")
        if ver == (8, 7):
            detail += " (8.7 can crash verifying signatures; update if `gatewright verify` fails)"
        checks.append(Check("ssh-keygen (signing)", ok, detail,
                            "Signing needs OpenSSH 8.2+: Debian/Ubuntu/Kali `sudo apt install openssh-client`, "
                            "Fedora `sudo dnf install openssh-clients`, Alpine `apk add openssh-keygen`",
                            required=cfg.required))
        checks.append(Check("signing key", cfg.key_path.exists(), str(cfg.key_path),
                            "Create one: gatewright keygen --identity you@example.com --add", required=cfg.required))
        asp = cfg.allowed_signers_path(pipeline.root)
        checks.append(Check("allowed signers", asp.exists(), str(asp),
                            "Add trusted public keys (gatewright keygen prints the line)", required=cfg.required))
    if pipeline and pipeline.vcs:
        import re as _re
        from .secrets import SecretStore
        v = tool_version("git") if git else ""
        m = _re.search(r"(\d+)\.(\d+)", v)
        gv = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        checks.append(Check("git >= 2.31 (watcher)", gv >= (2, 31), v or "not found",
                            "The watcher passes the token to git through the environment, which needs git 2.31+"))
        secrets = SecretStore(pipeline.root)
        tok = pipeline.vcs.get("token_secret", "VCS_TOKEN")
        checks.append(Check(f"vcs token ({tok})", secrets.get(tok) is not None, "set" if secrets.get(tok) else "not set",
                            f"Set GATEWRIGHT_SECRET_{tok} or add {tok}=... to .gatewright/secrets.env"))
        wh = pipeline.vcs.get("webhook")
        if wh:
            ws = wh.get("secret", "VCS_WEBHOOK_SECRET")
            checks.append(Check(f"webhook secret ({ws})", secrets.get(ws) is not None,
                                "set" if secrets.get(ws) else "not set", f"Set GATEWRIGHT_SECRET_{ws}"))
    if pipeline:
        from .runner import SHELL_BUILTINS
        tools: dict[str, list] = {}   # tool -> the jobs that use it (first job decides availability)
        for j in pipeline.jobs:
            name = j.tool_name
            if j.image or not name or name in ("python", "python3") or name in SHELL_BUILTINS or "=" in name:
                continue
            tools.setdefault(name, []).append(j)
        for name, jobs in tools.items():
            ok = tool_available(jobs[0], pipeline.root)
            checks.append(Check(name, ok, tool_version(name, pipeline.root) or ("available" if ok else "not found"),
                                hint(name),
                                # required if *any* job needs it, not just the first one seen
                                required=any(not j.optional for j in jobs)))
    return checks
