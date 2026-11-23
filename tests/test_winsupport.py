# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Windows support: the decisions on every OS, the real behaviour on Windows only."""

import os
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import psutil
import pytest

from gatewright import artifacts, doctor, errors, runner, winsupport
from gatewright.config import load_pipeline
from gatewright.engine import Engine, Selection
from gatewright.models import PASSED, JobError
from gatewright.secrets import SecretStore

from .conftest import BASIC_PIPELINE, PY, needs_posix_shell

windows_only = pytest.mark.skipif(os.name != "nt", reason="real Windows only")
INSTALL = Path(__file__).resolve().parents[1] / "install.ps1"


# ---------------------------------------------------------------- W8: long paths
def test_extended_paths():
    assert winsupport.extended("C:\\a\\b") == "\\\\?\\C:\\a\\b"
    assert winsupport.extended("\\\\srv\\share\\x") == "\\\\?\\UNC\\srv\\share\\x"
    assert winsupport.extended("\\\\?\\C:\\a") == "\\\\?\\C:\\a"


def test_posix_is_unchanged(monkeypatch):
    monkeypatch.setattr(winsupport.sys, "platform", "linux")
    assert winsupport.long_path("a/b") == Path("a/b")
    assert winsupport.long_paths_enabled() is None


@pytest.mark.parametrize("value", [True, False])
def test_doctor_reports_long_paths(monkeypatch, value):
    monkeypatch.setattr(doctor, "long_paths_enabled", lambda: value)
    c = {c.name: c for c in doctor.run_doctor(None)}["long paths"]
    assert c.ok is value and not c.required
    monkeypatch.setattr(doctor, "long_paths_enabled", lambda: None)
    assert "long paths" not in {c.name for c in doctor.run_doctor(None)}


def test_outputs_and_artifact_with_long_paths(tmp_path):
    """Regression (W8): over 260 characters, copying outputs and packing failed on Windows."""
    root, run_dir = tmp_path / "proj", tmp_path / "runs" / "r1"
    deep = "/".join(["d" * 60] * 5)
    src = winsupport.long_path(root) / Path(deep)
    src.mkdir(parents=True)
    (src / "out.txt").write_text("hi")
    run_dir.mkdir(parents=True)
    try:
        got = artifacts.collect_outputs(str(root), run_dir, "job", ["**/*.txt", "./d*/**/nothing", "../*"])
        assert got == [f"out/job/{deep}/out.txt"]
        artifacts.write_checksums(run_dir)
        assert artifacts.verify(run_dir) == []
        with tarfile.open(artifacts.pack(run_dir)) as tar:
            assert f"r1/out/job/{deep}/out.txt" in tar.getnames()
    finally:   # pytest's own cleanup can't delete long paths on Windows
        shutil.rmtree(winsupport.long_path(root))
        shutil.rmtree(winsupport.long_path(run_dir))


# ---------------------------------------------------------------- W9: path case
def test_true_case(tmp_path):
    (tmp_path / "App").mkdir()
    (tmp_path / "App" / "Calc.py").write_text("x = 1\n")
    root = str(tmp_path)
    assert winsupport.true_case(root, "App/Calc.py") == "App/Calc.py"
    assert winsupport.true_case(root, "app/CALC.py").lower() == "app/calc.py"
    assert winsupport.true_case(root, "app/CALC.py").startswith("App/")
    assert winsupport.true_case(root, "gone/x.py") == "gone/x.py"


def test_case_does_not_change_the_fingerprint_on_windows(tmp_path, monkeypatch):
    (tmp_path / "App").mkdir()
    (tmp_path / "App" / "c.py").write_text("x = 1\n")
    monkeypatch.setattr(errors.sys, "platform", "win32")
    a = errors.finalize(JobError("j", "lint", "low", "h", file="App/c.py", line=1, rule="R"), str(tmp_path))
    b = errors.finalize(JobError("j", "lint", "low", "h", file="app/c.py", line=1, rule="R"), str(tmp_path))
    assert b.file == "App/c.py" and a.fingerprint == b.fingerprint


# ---------------------------------------------------------------- W10: secrets file
@pytest.mark.parametrize("sddl, who", [
    ("D:P(A;;FA;;;S-1-5-21-1-2-3-1001)", []),
    ("D:AI(A;ID;0x1301bf;;;BU)(A;ID;FA;;;SY)(A;ID;FA;;;BA)", ["Users"]),
    ("D:(A;;FR;;;WD)(A;;GR;;;AU)(A;;FR;;;WD)", ["Everyone", "Authenticated Users"]),
    ("D:(A;;0x100000;;;BU)", []),                       # synchronize only: no read
    ("D:(A;;DCLCRP;;;BU)", []),                         # write, but not read
    ("D:(A;;CCDC;;;BU)", ["Users"]),
    ("D:(A;OICIIO;FA;;;WD)", []),                       # only for children
    ("D:(D;;FA;;;BU)(A;;FA;;;BU)", []),                 # denied first
    ("D:(A;;FR;;;S-1-5-21-9-9-9-513)", ["Domain Users"]),
    ("D:NO_ACCESS_CONTROL", ["Everyone"]),
    ("O:BA", ["Everyone"]),
    ("D:(A;;FA)", []),                                  # malformed
])
def test_broad_readers(sddl, who):
    assert winsupport.broad_readers(sddl) == who


def test_secrets_file_warning():
    w = winsupport.secrets_file_warning("C:\\p\\secrets.env", "D:(A;;FR;;;BU)", "me")
    assert "readable by other users (Users)" in w and 'icacls "C:\\p\\secrets.env"' in w and '"me:F" /remove "*S-1-5-32-545"' in w
    assert winsupport.secrets_file_warning("x", "D:(A;;FA;;;SY)", "me") is None
    assert "could not be read" in winsupport.secrets_file_warning("x", None, "me")


@windows_only
def test_real_secrets_file_permissions(tmp_path):
    f = tmp_path / ".gatewright" / "secrets.env"
    f.parent.mkdir()
    f.write_text("A=bcdefg\n")
    subprocess.run(["icacls", str(f), "/grant", "*S-1-1-0:R"], check=True, capture_output=True)
    (warning,) = SecretStore(str(tmp_path)).warnings
    assert "readable by other users (Everyone)" in warning
    fix = warning.split("Fix with: ")[1]                 # the advice must work as printed
    subprocess.run(fix, check=True, capture_output=True, shell=True)
    s = SecretStore(str(tmp_path))
    assert s.warnings == [] and s.get("A") == "bcdefg"
    assert winsupport.file_sddl(tmp_path / "missing") is None


# ---------------------------------------------------------------- W6: stopping a job
class _Proc:
    pid = 4242

    def __init__(self, code=None, own=True):
        self.code = code
        setattr(self, winsupport.OWN_GROUP, own)

    def poll(self):
        return self.code


def test_request_stop(monkeypatch):
    sent = []
    monkeypatch.setattr(signal, "CTRL_BREAK_EVENT", 1, raising=False)
    monkeypatch.setattr(winsupport.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    assert winsupport.request_stop(_Proc()) and sent == [(4242, 1)]
    assert not winsupport.request_stop(_Proc(code=0))        # already ended
    assert not winsupport.request_stop(_Proc(own=False))     # not our group: could hit Gatewright itself
    assert len(sent) == 1

    def no_console(pid, sig):
        raise OSError("no console")
    monkeypatch.setattr(winsupport.os, "kill", no_console)
    assert not winsupport.request_stop(_Proc())
    monkeypatch.delattr(signal, "CTRL_BREAK_EVENT")
    assert not winsupport.request_stop(_Proc())


def test_kill_tree_waits_after_a_stop_request(monkeypatch):
    waits = []
    monkeypatch.setattr(runner.os, "name", "nt")
    monkeypatch.setattr(runner, "request_stop", lambda p: True)

    def gone(pid):
        raise psutil.NoSuchProcess(pid)
    monkeypatch.setattr(runner.psutil, "Process", gone)
    monkeypatch.setattr(runner.psutil, "wait_procs", lambda procs, timeout: (waits.append(timeout), ([], []))[1])

    class P(_Proc):
        def wait(self, timeout):
            return 0
    runner._kill_tree(P())
    assert waits == [runner.STOP_GRACE, 3]


CLEANUP = """\
import signal, sys, time
def stop(*a):
    open('cleaned.txt', 'w').close()
    sys.exit(1)
signal.signal(signal.SIGBREAK, stop)
open('started.txt', 'w').close()
while True:
    time.sleep(0.1)
"""


@windows_only
@pytest.mark.parametrize("shell", ["auto", "cmd", "powershell"])
def test_real_job_can_clean_up_when_stopped(project, shell):
    root = project(BASIC_PIPELINE, f"""\
        jobs:
          - {{id: slow, gate: commit, shell: {shell}, run: "{PY} slow.py", timeout: 6}}
    """, files={"slow.py": CLEANUP})
    r = Engine(load_pipeline(root)).run(Selection(reports=["md"]))
    (job,) = r.all_jobs()
    assert job.errors[0].category == "timeout"
    assert (root / "started.txt").exists()
    assert (root / "cleaned.txt").exists()


# ---------------------------------------------------------------- W4: python without a project venv
def test_shim_files_per_os():
    assert runner.shim_files("python", "/opt/my venv/python", "posix") == [
        ("python", "#!/bin/sh\nexec '/opt/my venv/python' \"$@\"\n")]
    sh, cmd = runner.shim_files("python", "C:\\a b%c\\python.exe", "nt")
    assert sh == ("python", "#!/bin/sh\nexec 'C:/a b%c/python.exe' \"$@\"\n")
    assert cmd == ("python.cmd", '@"C:\\a b%%c\\python.exe" %*\r\n')


@windows_only
@pytest.mark.parametrize("shell", ["auto", "cmd", "powershell"])
def test_real_python_without_project_venv(project, shell, monkeypatch):
    """`python` is Gatewright's, in every shell; exit codes and arguments survive the wrapper."""
    monkeypatch.setattr(runner, "gatewright_tools_dir", lambda: os.path.dirname(sys.executable))
    root = project(BASIC_PIPELINE, f"""\
        jobs:
          - {{id: ok, gate: commit, shell: {shell}, run: 'python show.py "two words" a=b'}}
          - {{id: bad, gate: commit, shell: {shell}, run: 'python -c "raise SystemExit(7)"'}}
          - {{id: mod, gate: commit, shell: {shell}, run: "python -m psutil_missing_xyz"}}
    """, files={"show.py": "import sys\nprint('PREFIX=' + sys.prefix, sys.argv[1:])\n"})
    r = Engine(load_pipeline(root)).run(Selection(reports=["md"]))
    jobs = {j.job_id: j for j in r.all_jobs()}
    log = (Path(r.artifact_dir) / "logs" / "ok.log").read_text(encoding="utf-8")
    assert jobs["ok"].status == PASSED, log
    assert f"PREFIX={sys.prefix} ['two words', 'a=b']" in log
    assert jobs["bad"].exit_code == (1 if shell == "powershell" else 7)   # `powershell -Command` reports any failure as 1
    assert jobs["mod"].status == "error"                  # missing module found before the job starts


@windows_only
@needs_posix_shell
def test_real_wrapped_python_is_killed_on_timeout(project, monkeypatch):
    """The wrapper must not hide the real process from the timeout."""
    monkeypatch.setattr(runner, "gatewright_tools_dir", lambda: os.path.dirname(sys.executable))
    root = project(BASIC_PIPELINE, """\
        jobs:
          - {id: slow, gate: commit, run: "python pid.py", timeout: 5}
    """, files={"pid.py": "import os, time\nopen('pid.txt', 'w').write(str(os.getpid()))\ntime.sleep(60)\n"})
    r = Engine(load_pipeline(root)).run(Selection(reports=["md"]))
    assert r.all_jobs()[0].errors[0].category == "timeout"
    pid = int((root / "pid.txt").read_text(encoding="utf-8"))
    for _ in range(30):
        if not psutil.pid_exists(pid):
            break
        time.sleep(0.1)
    assert not psutil.pid_exists(pid)


def test_your_own_tools_come_before_gatewrights(tmp_path, monkeypatch):
    """Regression (W4): on Windows, Gatewright's ruff/pip/pytest shadowed your own."""
    suffix = ".exe" if os.name == "nt" else ""
    mine, tools = tmp_path / "mine", tmp_path / "tools"
    for d in (mine, tools):
        d.mkdir()
        (d / f"ruff{suffix}").write_text("")
        (d / f"ruff{suffix}").chmod(0o755)
    (tools / f"bandit{suffix}").write_text("")
    (tools / f"bandit{suffix}").chmod(0o755)
    monkeypatch.setattr(runner, "gatewright_tools_dir", lambda: str(tools))
    path = runner.job_path(str(tmp_path), base=str(mine))
    assert Path(shutil.which("ruff", path=path)).parent == mine
    assert Path(shutil.which("bandit", path=path)).parent == tools


# ---------------------------------------------------------------- W17: install.ps1
def _install(*args):
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(INSTALL), *args],
                          capture_output=True, text=True, timeout=120)


@windows_only
def test_install_ps1_dry_run_changes_nothing(tmp_path):
    p = _install("-DryRun", "-Prefix", str(tmp_path / "v"), "-BinDir", str(tmp_path / "b"), "-Python", sys.executable)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "-m venv" in p.stdout and "[tools]" in p.stdout and list(tmp_path.iterdir()) == []
    p = _install("-DryRun", "-NoTools", "-Prefix", str(tmp_path / "v"), "-Python", sys.executable)
    assert "[tools]" not in p.stdout


@windows_only
def test_install_ps1_never_touches_other_folders(tmp_path):
    (tmp_path / "keep.txt").write_text("x")
    for args in (["-Uninstall"], ["-Python", sys.executable]):
        p = _install(*args, "-Prefix", str(tmp_path), "-BinDir", str(tmp_path / "b"))
        assert p.returncode != 0 and (tmp_path / "keep.txt").exists(), p.stdout


# ---------------------------------------------------------------- redirected output
def test_output_to_a_legacy_code_page_does_not_crash(project):
    """Regression: `gatewright run > log.txt` crashed on Windows on the first symbol."""
    root = project(BASIC_PIPELINE, "jobs:\n  - {id: a, gate: commit, run: 'echo hi'}\n")
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    p = subprocess.run([sys.executable, "-m", "gatewright", "-C", str(root), "run"], env=env, capture_output=True,
                       cwd=Path(__file__).resolve().parents[1])
    assert p.returncode == 0, p.stdout + p.stderr
    assert b"charmap" not in p.stdout + p.stderr


def test_lenient_output_with_other_streams(monkeypatch):
    from gatewright import cli

    class Stream:                       # e.g. a GUI's replacement for stdout
        pass
    calls = []

    class Text(Stream):
        def reconfigure(self, **kw):
            calls.append(kw)
    monkeypatch.setattr(sys, "stdout", Stream())
    monkeypatch.setattr(sys, "stderr", Text())
    cli._lenient_output()
    assert calls == [{"errors": "replace"}]
