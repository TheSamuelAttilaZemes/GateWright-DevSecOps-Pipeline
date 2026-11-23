# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""`gatewright doctor` checks, in-process, with the machine's answers faked where needed."""

import subprocess
import sys

import pytest

from gatewright import doctor, platform_info, signing
from gatewright.config import load_pipeline

from .conftest import BASIC_PIPELINE



def checks_by_name(checks):
    return {c.name: c for c in checks}


def test_without_pipeline():
    c = checks_by_name(doctor.run_doctor(None))
    assert {"os", "python", "git", "python for jobs", "shell", "docker", "weasyprint (PDF)"} <= set(c)
    assert c["python"].ok and not c["docker"].required


def test_tool_required_if_any_job_needs_it(project):
    """Regression: the first job decided, so an optional job hid a required job's missing tool."""
    root = project(BASIC_PIPELINE, """\
jobs:
  - {id: a, gate: commit, run: 'not-installed-tool-xyz --fast', optional: true}
  - {id: b, gate: test, run: 'not-installed-tool-xyz --full'}
  - {id: c, gate: test, run: 'cd src && echo hi'}
  - {id: d, gate: test, run: 'FOO=1 make'}
  - {id: e, gate: test, run: 'other-missing-xyz', optional: true}
""")
    c = checks_by_name(doctor.run_doctor(load_pipeline(root)))
    assert c["not-installed-tool-xyz"].required and not c["not-installed-tool-xyz"].ok
    assert not c["other-missing-xyz"].required
    assert "cd" not in c and "FOO=1" not in c          # regression: builtins/env prefixes listed as tools


def test_docker_states(project, monkeypatch):
    root = project(BASIC_PIPELINE, "jobs:\n  - {id: a, gate: commit, run: 'echo', image: 'alpine:3'}\n")
    p = load_pipeline(root)
    monkeypatch.setattr(doctor.shutil, "which", lambda n, **k: None if n == "docker" else "/usr/bin/" + n)
    c = checks_by_name(doctor.run_doctor(p))
    assert c["docker"].required and c["docker"].detail == "not found"
    monkeypatch.setattr(doctor.shutil, "which", lambda n, **k: "/usr/bin/" + n)

    def no_daemon(*a, **k):
        raise subprocess.TimeoutExpired("docker", 10)
    monkeypatch.setattr(doctor.subprocess, "run", no_daemon)
    assert "not reachable" in checks_by_name(doctor.run_doctor(p))["docker"].detail


def test_git_missing(monkeypatch):
    real = doctor.shutil.which
    monkeypatch.setattr(doctor.shutil, "which", lambda n, **k: None if n == "git" else real(n, **k))
    assert "not found" in checks_by_name(doctor.run_doctor(None))["git"].detail


def test_unknown_os_and_wsl(monkeypatch):
    osi = platform_info.OSInfo(system="Linux", distro_id="mystery", id_like=[], name="Mystery OS", version="",
                               family="unknown", package_manager="", wsl=True, externally_managed=False)
    monkeypatch.setattr(platform_info, "detect", lambda *a, **k: osi)
    c = checks_by_name(doctor.run_doctor(None))
    assert not c["os"].ok and not c["os"].required and "WSL" in c["os"].detail


def test_windows_without_git_bash(monkeypatch):
    monkeypatch.setattr(doctor.os, "name", "nt")
    monkeypatch.setattr("gatewright.runner.shell_command", lambda cmd, sh: ([], "", "cmd"))
    c = checks_by_name(doctor.run_doctor(None))
    assert not c["shell"].ok and "Git Bash" in c["shell"].detail


def test_pdf_states(monkeypatch):
    monkeypatch.setattr(doctor, "_py_module", lambda n: False)
    assert "not installed" in checks_by_name(doctor.run_doctor(None))["weasyprint (PDF)"].detail
    monkeypatch.setattr(doctor, "_py_module", lambda n: True)
    monkeypatch.setattr(doctor, "weasyprint_problem", lambda: "cannot load library 'pango'")
    c = checks_by_name(doctor.run_doctor(None))["weasyprint (PDF)"]
    assert not c.ok and "system library missing" in c.detail
    monkeypatch.setattr(doctor, "weasyprint_problem", lambda: None)
    assert checks_by_name(doctor.run_doctor(None))["weasyprint (PDF)"].ok


def test_weasyprint_problem_reports_import_errors(monkeypatch):
    monkeypatch.setitem(sys.modules, "weasyprint", None)        # import raises ImportError
    assert doctor.weasyprint_problem()


@pytest.mark.parametrize("ver, ok, note", [((9, 6), True, ""), ((8, 7), True, "8.7"), ((7, 4), False, ""),
                                           (None, True, "found")])
def test_signing_checks(project, monkeypatch, tmp_path, ver, ok, note):
    root = project(BASIC_PIPELINE + f"signing:\n  key: {tmp_path / 'k'}\n  identity: me@x\n  required: true\n",
                   "jobs: []\n")
    monkeypatch.setattr(signing, "openssh_version", lambda: ver)
    c = checks_by_name(doctor.run_doctor(load_pipeline(root)))
    assert c["ssh-keygen (signing)"].ok is ok and note in c["ssh-keygen (signing)"].detail
    assert not c["signing key"].ok and c["signing key"].required
    assert not c["allowed signers"].ok


def test_vcs_checks(project, monkeypatch):
    root = project(BASIC_PIPELINE + "vcs:\n  provider: github\n  repo: a/b\n  webhook: {port: 9999}\n",
                   "jobs: []\n")
    monkeypatch.setenv("GATEWRIGHT_SECRET_VCS_TOKEN", "t")
    c = checks_by_name(doctor.run_doctor(load_pipeline(root)))
    assert c["vcs token (VCS_TOKEN)"].ok
    assert not c["webhook secret (VCS_WEBHOOK_SECRET)"].ok
    assert "git >= 2.31 (watcher)" in c
    monkeypatch.setattr(doctor, "tool_version", lambda *a: "")
    assert not checks_by_name(doctor.run_doctor(load_pipeline(root)))["git >= 2.31 (watcher)"].ok
