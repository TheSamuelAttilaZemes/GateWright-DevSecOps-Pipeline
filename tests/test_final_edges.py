# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The last uncovered branches: Python environments, kill paths, engine status rules, CLI printing."""

import os
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from gatewright import cli, runner
from gatewright.config import ConfigError, load_pipeline
from gatewright.engine import Engine, PlannedGate, Selection, git_info
from gatewright.models import Job, fmt_bytes
from gatewright.secrets import SecretStore

from .conftest import BASIC_PIPELINE, posix_only

from .conftest import PY  # noqa: E402


# ---------------------------------------------------------------- runner: Python environments
@posix_only
def test_shims_and_tools_dir_when_running_in_a_venv(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "base_prefix", "/somewhere/else")
    monkeypatch.setattr(runner, "_SHIMS", None)
    assert runner.gatewright_tools_dir() == os.path.dirname(sys.executable)
    shims = runner._python_shims()
    assert os.access(os.path.join(shims, "python"), os.X_OK)
    assert runner._python_shims() == shims                              # created once
    exe, src = runner.python_for_jobs(str(tmp_path))
    assert exe == sys.executable and "Gatewright's environment" in src
    assert os.path.dirname(sys.executable) in runner.job_path(str(tmp_path), base="/usr/bin")


def test_project_venv_is_preferred(tmp_path):
    venv = tmp_path / ".venv"
    sub, name = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")  # real venv layout per OS
    (venv / sub).mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr\n")
    py = venv / sub / name
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    exe, src = runner.python_for_jobs(str(tmp_path))
    assert src == "project venv" and os.path.normcase(exe) == os.path.normcase(str(py))  # PATHEXT: .EXE
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "pyvenv.cfg").write_text("")
    assert runner.project_venv(str(tmp_path / "x")) is None


def test_windows_bash_lookup(monkeypatch, tmp_path):
    git_bash = tmp_path / "Git" / "bin" / "bash.exe"
    git_bash.parent.mkdir(parents=True)
    git_bash.write_text("")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    assert runner._windows_bash(lambda n: r"C:\Windows\System32\bash.exe") == str(git_bash)   # not WSL's bash
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "none"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "none"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "none"))
    assert runner._windows_bash(lambda n: None) is None


def test_expand_vars_skips_none_and_foreign_keys():
    assert runner.expand_gatewright_vars("$GATEWRIGHT_OUT $HOME", {"GATEWRIGHT_OUT": None, "HOME": "x"}) == \
        "$GATEWRIGHT_OUT $HOME"


def test_docker_argv_user_and_env(monkeypatch, tmp_path):
    job = Job(id="a", gate="g", run="make", image="alpine", env={"MODE": "ci"})
    monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(os, "getgid", lambda: 1000, raising=False)
    argv, _ = runner._docker_argv(job, str(tmp_path), "r", str(tmp_path / "o"), ["TOKEN"])
    assert "--user" in argv and argv[argv.index("--user") + 1] == "1000:1000"
    assert ["-e", "MODE"] == argv[argv.index("MODE") - 1:argv.index("MODE") + 1] and "TOKEN" in argv


class _Proc:
    """A process whose children refuse to die politely."""

    def __init__(self):
        self.pid = 999999
        self.killed = False

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("x", timeout)

    def kill(self):
        self.killed = True

    def poll(self):
        return None


def test_kill_tree_escalates(monkeypatch):
    class Stubborn:
        def terminate(self):
            raise psutil.NoSuchProcess(1)

        def kill(self):
            raise psutil.AccessDenied(1)

    class Parent(Stubborn):
        def children(self, recursive):
            return [Stubborn()]
    monkeypatch.setattr(runner.psutil, "Process", lambda pid: Parent())
    monkeypatch.setattr(runner.psutil, "wait_procs", lambda procs, timeout: ([], procs))
    p = _Proc()
    runner._kill_tree(p)
    assert p.killed                                   # wait() timed out -> hard kill


def test_kill_tree_when_process_lookup_fails(monkeypatch):
    def gone(pid):
        raise psutil.NoSuchProcess(pid)
    monkeypatch.setattr(runner.psutil, "Process", gone)
    monkeypatch.setattr(runner.psutil, "wait_procs", lambda procs, timeout: ([], []))
    runner._kill_tree(_Proc())


def test_mask_skips_large_files(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWRIGHT_SECRET_T", "topsecret")
    s = SecretStore(str(tmp_path))
    s.get("T")
    big = tmp_path / "big.txt"
    big.write_text("topsecret")
    monkeypatch.setattr(runner, "MASK_FILE_LIMIT", 3)
    assert runner.mask_output_files(str(tmp_path), s) == 0 and big.read_text(encoding="utf-8") == "topsecret"


def _exec(tmp_path, job):
    return runner.execute(job, root=str(tmp_path), run_id="r", out_dir=str(tmp_path / "out"),
                          log_file=str(tmp_path / "log"), secrets=SecretStore(str(tmp_path)),
                          handle=runner.JobHandle(job))


def test_optional_tool_missing_inside_the_command_is_skipped(tmp_path):
    job = Job(id="o", gate="g", run="sh -c 'exit 127'", optional=True)
    r = _exec(tmp_path, job)
    assert r.status == "skipped"


def test_unreadable_report_and_generic_fallback(tmp_path, monkeypatch):
    real = Path.read_text

    def deny(self, *a, **k):
        if self.name == "r.json":
            raise PermissionError("denied")
        return real(self, *a, **k)
    (tmp_path / "w.py").write_text("import os, sys\nopen(os.environ['GATEWRIGHT_OUT'] + '/r.json', 'w').write('[]')\n"
                                   "print('boom')\nsys.exit(3)\n")
    monkeypatch.setattr(Path, "read_text", deny)
    r = _exec(tmp_path, Job(id="w", gate="g", run=f'"{PY}" w.py',
                            parser={"type": "ruff", "file": "$GATEWRIGHT_OUT/r.json"}))
    assert r.status == "error"                        # no report: parser can't read stdout either
    monkeypatch.undo()
    (tmp_path / "e.py").write_text("print('[]')\nraise SystemExit(4)\n")
    r = _exec(tmp_path, Job(id="e", gate="g", run=f'"{PY}" e.py', parser={"type": "ruff"}))
    assert r.status == "failed" and r.errors[0].category == "job_failure"       # generic fallback
    assert "exit code 4" in r.errors[0].human


# ---------------------------------------------------------------- engine status rules
def _eng(project, pipeline, jobs, **kw):
    root = project(pipeline, jobs, **kw)
    return root, Engine(load_pipeline(root))


def test_planned_gate_jobs_and_git_info_without_git(tmp_path, monkeypatch):
    a, b = Job(id="a", gate="g", run="x"), Job(id="b", gate="g", run="x")
    assert PlannedGate("g", "enforced", [[a], [b]]).jobs == [a, b]

    def no_git(*a, **k):
        raise OSError("no git")
    monkeypatch.setattr("gatewright.engine.subprocess.run", no_git)
    assert git_info(str(tmp_path)) == ("", "", False)


def test_advisory_gate_with_unmet_requires_runs_with_a_warning(project):
    pipe = "version: 1\nname: t\ngates:\n  - {id: a}\n  - {id: b, mode: advisory, requires: [a]}\n"
    root, eng = _eng(project, pipe, f"jobs:\n  - {{id: x, gate: b, run: '{PY} -c \"print(1)\"'}}\n", git=True)
    run = eng.run(Selection(gates=["b"], reports=["md"]))
    assert run.status == "passed" and any("requires" in w or "gate" in w for w in run.warnings)


def test_skipped_and_warn_gates_and_run(project):
    ruff_json = '[{"code": "E501", "message": "long", "filename": "a.py", "location": {"row": 1}}]'
    jobs = f"""\
jobs:
  - {{id: maybe, gate: commit, run: 'not-installed-xyz', optional: true}}
  - id: lint
    gate: test
    run: '{PY} -c "print(open(\\"o.json\\").read()); raise SystemExit(1)"'
    parser: {{type: ruff}}
    fail_on: high
"""
    root, eng = _eng(project, BASIC_PIPELINE, jobs, files={"o.json": ruff_json, "a.py": "x = 1\n"})
    run = eng.run(Selection(reports=["md"]))
    gates = {g.gate: g.status for g in run.gates}
    assert gates == {"commit": "skipped", "test": "warn"} and run.status == "warn"
    run = eng.run(Selection(gates=["commit"], reports=["md"]))
    assert run.status == "skipped"                          # every gate skipped


def test_resume_on_a_different_commit_warns(project):
    root, eng = _eng(project, BASIC_PIPELINE, f"jobs:\n  - {{id: f, gate: commit, run: '{PY} f.py'}}\n",
                     files={"f.py": "raise SystemExit(1)\n"}, git=True)
    first = eng.run(Selection(reports=["md"]))
    (root / "f.py").write_text("print('fixed')\n")
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "fix"], cwd=root, check=True)
    run = Engine(load_pipeline(root)).rerun_failed(first.run_id)
    assert any("made on commit" in w for w in run.warnings)


def test_pdf_warning_is_written_into_the_reports(project, monkeypatch):
    from gatewright import reports
    monkeypatch.setattr(reports, "write_pdf", lambda html, path: "PDF export needs WeasyPrint")
    root, eng = _eng(project, BASIC_PIPELINE, f"jobs:\n  - {{id: a, gate: commit, run: '{PY} -c \"1\"'}}\n")
    run = eng.run(Selection(reports=["md", "pdf"]))
    assert "PDF export needs WeasyPrint" in (Path(run.artifact_dir) / "report.md").read_text(encoding="utf-8")


def test_notifications_from_the_engine_respect_auto_off(project):
    pipe = BASIC_PIPELINE + "notify:\n  channels:\n    - {type: webhook, url: 'http://127.0.0.1:9/x'}\n"
    root, eng = _eng(project, pipe, f"jobs:\n  - {{id: a, gate: commit, run: '{PY} -c \"1\"'}}\n")
    eng.run(Selection(reports=["md"]))
    sign_off = BASIC_PIPELINE + "signing:\n  auto: false\n  identity: me@x\n"
    root2 = project(sign_off, f"jobs:\n  - {{id: a, gate: commit, run: '{PY} -c \"1\"'}}\n")
    run = Engine(load_pipeline(root2)).run(Selection(reports=["md"]))
    assert not (Path(run.artifact_dir) / "checksums.sha256.sig").exists()


def test_invalid_yaml_pipeline(project):
    root = project("gates: [unclosed\n", "jobs: []\n")
    with pytest.raises(ConfigError):
        load_pipeline(root)


# ---------------------------------------------------------------- CLI printing details
def test_progress_prints_policy_and_messages(capsys):
    from gatewright.models import GateResult
    prog = cli._Progress(verbose=False)
    g = GateResult(gate="commit", mode="enforced")
    g.policy = [{"human": "no criticals", "passed": True, "skipped": False},
                {"human": "x", "passed": False, "skipped": True}]
    prog("gate_end", {"gate": g})
    for level in ("error", "info", "warning"):
        prog("message", {"level": level, "text": f"{level} text"})
    out = capsys.readouterr().out
    assert "policy: no criticals" in out and "✓ info text" in out and "! error text" in out


def test_result_lists_at_most_30_errors_and_run_policy(capsys, tmp_path):
    from gatewright.models import GateResult, JobError, JobResult, RunResult
    run = RunResult(run_id="r", pipeline="p", root=str(tmp_path), started="", artifact_dir=str(tmp_path))
    j = JobResult("lint", "commit", "dev", "failed")
    j.errors = [JobError("lint", "lint", "low", f"issue {i}") for i in range(35)]
    g = GateResult(gate="commit", mode="enforced")
    g.jobs = [j]
    run.gates = [g]
    run.policy = [{"human": "coverage >= 80", "passed": False, "skipped": False},
                  {"human": "skipped rule", "passed": True, "skipped": True}]
    cli._print_result(run)
    out = capsys.readouterr().out
    assert "... and 5 more" in out and "run policy: coverage >= 80" in out and "skipped rule" not in out


def test_plan_prints_warnings(project, capsys, monkeypatch):
    root = project(BASIC_PIPELINE, f"jobs:\n  - {{id: a, gate: commit, run: '{PY} -c \"1\"'}}\n")
    real = Engine.plan

    def with_warning(self, sel):
        p = real(self, sel)
        p.warnings.append("watch out")
        return p
    monkeypatch.setattr(Engine, "plan", with_warning)
    assert cli.main(["-C", str(root), "plan"]) == 0 and "! watch out" in capsys.readouterr().out


def test_notify_test_reports_each_sent_channel(project, capsys, monkeypatch):
    from gatewright.notify import Notifier
    monkeypatch.setattr(Notifier, "_post", staticmethod(lambda *a, **k: None))
    root = project(BASIC_PIPELINE + "notify:\n  channels:\n    - {type: webhook, url: 'https://h/x'}\n", "jobs: []\n")
    assert cli.main(["-C", str(root), "notify-test"]) == 0 and "✓ webhook" in capsys.readouterr().out


def test_cli_as_a_script(tmp_path):
    out = subprocess.run([PY, "-m", "gatewright.cli", "--version"], capture_output=True, text=True,
                         cwd=Path(__file__).resolve().parents[1])
    assert out.returncode == 0 and "gatewright" in out.stdout


# ---------------------------------------------------------------- small helpers
def test_fmt_bytes_huge():
    assert fmt_bytes(5 * 1024**5) == "5120.0TB"


def test_queue_add_rolls_back_on_error(tmp_path, monkeypatch):
    from gatewright import queue as qmod
    from gatewright.vcs import VCSEvent
    q = qmod.Queue(str(tmp_path))
    monkeypatch.setattr(qmod, "_now", lambda: (_ for _ in ()).throw(RuntimeError("clock broke")))
    with pytest.raises(RuntimeError):
        q.add(VCSEvent("push", "d" * 40, "main"), True, False)
    monkeypatch.undo()
    assert q.list(None, 5) == []                          # nothing half-written


def test_cache_key_with_unreadable_file(tmp_path, monkeypatch):
    from gatewright.cache import Cache
    (tmp_path / "a.txt").write_text("x")
    c = Cache(str(tmp_path))
    real_open = open

    def deny(path, *a, **k):
        if str(path).endswith("a.txt"):
            raise PermissionError("denied")
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", deny)
    assert len(c._file_hash("a.txt")) == 64


def test_secrets_file_permissions_warning(tmp_path):
    f = tmp_path / ".gatewright" / "secrets.env"
    f.parent.mkdir()
    f.write_text("A=bcdefg\n")
    f.chmod(0o600)
    assert SecretStore(str(tmp_path)).warnings == []
    if os.name == "posix":
        f.chmod(0o644)
        assert "readable by other users" in SecretStore(str(tmp_path)).warnings[0]


def test_short_secrets_are_not_masked(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWRIGHT_SECRET_PIN", "12")
    s = SecretStore(str(tmp_path))
    s.get("PIN")
    assert s.mask("pin 12") == "pin 12" and not s.active      # too short to mask safely


def test_signature_check_when_ssh_keygen_fails(tmp_path, monkeypatch):
    from gatewright import signing
    (tmp_path / "checksums.sha256").write_text("")
    (tmp_path / "checksums.sha256.sig").write_text("sig")
    (tmp_path / "signature.json").write_text('{"identity": "me@x"}')
    allowed = tmp_path / "allowed"
    allowed.write_text("me@x ssh-ed25519 AAAA\n")
    monkeypatch.setattr(signing, "_run", lambda *a, **k: (_ for _ in ()).throw(signing.SigningError("ssh-keygen gone")))
    assert signing.verify(tmp_path, allowed) == (False, "ssh-keygen gone")


def test_release_notes_mention_coverage(project):
    from gatewright.releasenotes import generate
    cov = '{"totals": {"percent_covered": 87.5}}'
    root = project(BASIC_PIPELINE, f"jobs:\n  - {{id: cov, gate: test, run: '{PY} -c \"print(open(\\\"c.json\\\").read())\"', "
                                   "parser: {type: coverage}}\n", files={"c.json": cov}, git=True)
    run = Engine(load_pipeline(root)).run(Selection(reports=["md"]))
    assert "Coverage: 87.5%" in generate(str(root), None, "HEAD", run)


def test_watcher_logs_host_errors_instead_of_crashing(tmp_path):
    from gatewright.vcs import VCSError
    from gatewright.watch import Watcher
    (tmp_path / ".gatewright").mkdir()
    (tmp_path / ".gatewright" / "pipeline.yml").write_text(
        "version: 1\nname: a\ngates: [{id: commit}]\nvcs: {provider: github, repo: a/b}\n")
    logs = []
    w = Watcher(str(tmp_path), log=logs.append)

    def fail(*a):
        raise VCSError("403 rate limited")
    assert w._safe(fail) is False and logs == ["! 403 rate limited"]
