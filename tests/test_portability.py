# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Windows/cross-platform behaviour, tested on any OS by simulating Windows
inputs (paths, shells, process trees, non-UTF-8 locales)."""

import ntpath
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import psutil
import pytest

from gatewright import errors
from gatewright.config import load_pipeline
from gatewright.engine import Engine, Selection
from gatewright.models import JobError, PASSED
from gatewright.runner import _kill_tree, expand_gatewright_vars, shell_command

from .conftest import BASIC_PIPELINE, needs_posix_shell, posix_only

GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"


def fake_which(mapping):
    return lambda name: mapping.get(name)


def test_windows_prefers_git_bash():
    args, use_shell, name = shell_command("echo hi", "auto", os_name="nt", which=fake_which({"bash": GIT_BASH}))
    assert name == "bash" and args == [GIT_BASH, "-c", "echo hi"] and use_shell is False


def test_windows_ignores_wsl_bash(monkeypatch):
    monkeypatch.setattr("gatewright.runner.os.path.exists", lambda p: False)
    _, use_shell, name = shell_command("echo hi", "auto", os_name="nt",
                                       which=fake_which({"bash": r"C:\Windows\System32\bash.exe"}))
    assert name == "cmd" and use_shell is True


def test_windows_powershell_and_posix_default():
    args, _, name = shell_command("Get-Date", "powershell", os_name="nt",
                                  which=fake_which({"pwsh": r"C:\pwsh\pwsh.exe"}))
    assert name == "powershell" and args[0] == r"C:\pwsh\pwsh.exe" and args[-1] == "Get-Date"
    assert shell_command("ls", "auto", os_name="posix")[2] == "sh"


def test_gatewright_vars_expand_in_every_shell_syntax():
    v = {"GATEWRIGHT_OUT": "C:/p/.gatewright/runs/1/out/x", "GATEWRIGHT_RUN_ID": "1"}
    cmd = 'a "$GATEWRIGHT_OUT/r.json" ${GATEWRIGHT_OUT}/b %GATEWRIGHT_OUT%\\c $env:GATEWRIGHT_OUT/d $GATEWRIGHT_OUTPUT run=$GATEWRIGHT_RUN_ID'
    out = expand_gatewright_vars(cmd, v)
    assert out == ('a "C:/p/.gatewright/runs/1/out/x/r.json" C:/p/.gatewright/runs/1/out/x/b C:/p/.gatewright/runs/1/out/x\\c '
                   'C:/p/.gatewright/runs/1/out/x/d $GATEWRIGHT_OUTPUT run=1')


def test_cmd_style_variables_work_in_a_real_run(project):
    root = project(BASIC_PIPELINE, """\
        jobs:
          - id: rep
            gate: commit
            run: "{PY} -c \\"import sys; open(sys.argv[1], 'w').write('{}')\\" %GATEWRIGHT_OUT%/r.json"
            parser: {type: coverage, file: "%GATEWRIGHT_OUT%/r.json"}
          - {id: b, gate: commit, shell: bash, run: "echo bash-$GATEWRIGHT_JOB"}
    """)
    r = Engine(load_pipeline(root)).run(Selection(reports=["md"]))
    jobs = {j.job_id: j for j in r.all_jobs()}
    assert jobs["rep"].status == PASSED, jobs["rep"].errors
    assert jobs["b"].status == PASSED
    assert "bash-b" in (Path(r.artifact_dir) / "logs" / "b.log").read_text(encoding="utf-8")


def test_windows_traceback_location_regex():
    text = "C:\\proj\\tests\\test_calc.py:9: AssertionError\nE   assert 0.0 == 90"
    m = errors._LOC_PATTERNS[1].search(text)
    assert m and m.group(1) == "C:\\proj\\tests\\test_calc.py" and m.group(2) == "9"
    text2 = '  File "C:\\Users\\me\\proj\\app\\calc.py", line 12, in discount'
    m2 = errors._LOC_PATTERNS[0].search(text2)
    assert m2.group(1).endswith("calc.py") and m2.group(2) == "12"


def test_windows_paths_relative_and_forward_slashes(monkeypatch):
    monkeypatch.setattr(errors, "os", types.SimpleNamespace(path=ntpath))
    assert errors.relpath("C:\\proj\\app\\calc.py", "C:\\proj") == "app/calc.py"
    assert errors.relpath("file:///C:/proj/app/calc.py", "C:\\proj") == "app/calc.py"
    assert errors.relpath("D:\\other\\x.py", "C:\\proj") == "D:/other/x.py"  # other drive: no crash


def test_fingerprint_same_on_windows_and_linux(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "c.py").write_text("x = 1\n")
    a = errors.finalize(JobError("j", "lint", "low", "h", file="app/c.py", line=1, rule="R"), str(tmp_path))
    b = errors.finalize(JobError("j", "lint", "low", "h", file="app\\c.py", line=1, rule="R"), str(tmp_path))
    assert b.file == "app/c.py" and a.fingerprint == b.fingerprint


@posix_only
def test_kill_tree_without_process_group(monkeypatch):
    """On Windows there is no killpg; children of the shell must still die."""
    proc = subprocess.Popen(["sh", "-c", "sleep 100 & sleep 100 & wait"])
    time.sleep(0.5)
    kids = psutil.Process(proc.pid).children(recursive=True)
    assert len(kids) >= 2
    monkeypatch.setattr("gatewright.runner.os.name", "nt")
    _kill_tree(proc)
    monkeypatch.undo()
    time.sleep(0.3)
    assert proc.poll() is not None
    assert not [k for k in kids if k.is_running() and k.status() != psutil.STATUS_ZOMBIE]


@posix_only
def test_non_utf8_locale_does_not_crash(tmp_path, tmp_path_factory):
    """Windows defaults to cp1252; simulate a non-UTF-8 locale and write non-ASCII text."""
    code = f"""
import sys
sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
from pathlib import Path
from gatewright.templates import init
from gatewright.baseline import Baseline
from gatewright.models import RunResult, GateResult, JobResult, JobError
root = Path({str(tmp_path)!r})
init(root, "python")
e = JobError("j", "finding", "low", "Passord funnet i fil – sjekk æøå ✓", file="a.py", line=1, rule="B105", fingerprint="f1")
run = RunResult("r", "p", str(root), "now", gates=[GateResult("commit", "enforced", jobs=[JobResult("j", "commit", "sec", "failed", errors=[e])])])
Baseline.write_from_run(str(root), run, 30, "Godkjent av Øyvind ✓")
assert Baseline(str(root)).items[0].reason == "Godkjent av Øyvind ✓"
print("OK")
"""
    env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONIOENCODING": "ascii:replace"}
    env.pop("LC_CTYPE", None)
    script = tmp_path_factory.mktemp("scripts") / "locale_check.py"
    script.write_text("# -*- coding: utf-8 -*-\n" + code, encoding="utf-8")
    out = subprocess.run([sys.executable, "-X", "utf8=0", str(script)], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    assert "OK" in out.stdout


@pytest.mark.skipif(os.name != "nt", reason="real Windows only")
def test_real_windows_smoke(project):
    root = project(BASIC_PIPELINE, """\
        jobs:
          - {id: hello, gate: commit, run: "echo hello from windows"}
          - {id: slow, gate: commit, run: "{PY} -c \\"import time; time.sleep(30)\\"", timeout: 2}
    """)
    r = Engine(load_pipeline(root)).run(Selection(reports=["md", "html"]))
    jobs = {j.job_id: j for j in r.all_jobs()}
    assert jobs["hello"].status == PASSED
    assert jobs["slow"].errors[0].category == "timeout"


def test_jobs_use_project_venv(project, tmp_path):
    import venv
    root = project(BASIC_PIPELINE, """\
        jobs:
          - {id: which-python, gate: commit, run: "python -c \\"import sys; print('PREFIX=' + sys.prefix)\\""}
    """)
    venv.create(root / ".venv", with_pip=False, symlinks=os.name != "nt")
    r = Engine(load_pipeline(root)).run(Selection(reports=["md"]))
    log = (Path(r.artifact_dir) / "logs" / "which-python.log").read_text(encoding="utf-8")
    assert f"PREFIX={root / '.venv'}" in log, log


def test_job_path_order_without_project_venv(monkeypatch, tmp_path):
    from gatewright import runner
    tools = tmp_path / "gatewright-venv" / "bin"
    tools.mkdir(parents=True)
    monkeypatch.setattr(runner, "gatewright_tools_dir", lambda: str(tools))
    monkeypatch.setattr(runner, "_SHIMS", None)
    path = runner.job_path(str(tmp_path), base=os.pathsep.join(["/usr/local/bin", "/usr/bin"])).split(os.pathsep)
    if os.name != "nt":
        assert path[0].startswith(os.path.join(os.path.dirname(path[0]), "gatewright-shims-"))
        shim = Path(path[0]) / "python"
        assert sys.executable in shim.read_text(encoding="utf-8")
        out = subprocess.run([str(shim), "-c", "import sys; print(sys.prefix)"], capture_output=True, text=True)
        assert out.stdout.strip() == sys.prefix  # same environment as Gatewright, not the base Python
    if os.name == "nt":
        assert sorted(os.listdir(path[0])) == ["python", "python.cmd", "python3", "python3.cmd"]
    assert path[-1] == str(tools)            # Gatewright's tools come after the user's PATH
    assert path.index("/usr/bin") < path.index(str(tools))


def test_python_module_availability(project):
    from gatewright.runner import tool_available
    from gatewright.models import Job
    assert tool_available(Job(id="a", gate="g", run="python -m json.tool x"), None)
    assert not tool_available(Job(id="b", gate="g", run="python -m definitely_not_a_module_xyz"), None)


def test_git_bash_tool_dirs_from_either_bash_exe(tmp_path):
    from gatewright.runner import git_bash_tool_dirs
    git = tmp_path / "Git"
    for d in ("bin", "usr/bin", "mingw64/bin"):
        (git / d).mkdir(parents=True)
    want = [str(git / "usr" / "bin"), str(git / "mingw64" / "bin")]
    assert git_bash_tool_dirs(str(git / "bin" / "bash.exe")) == want        # the launcher
    assert git_bash_tool_dirs(str(git / "usr" / "bin" / "bash.exe")) == want  # the real bash
    assert git_bash_tool_dirs(None) == []
    assert git_bash_tool_dirs(str(tmp_path / "nowhere" / "bash.exe")) == []


def test_cmd_builtins_and_powershell_cmdlets_count_as_available(monkeypatch):
    from gatewright import runner
    from gatewright.models import Job
    monkeypatch.setattr(runner, "shell_command", lambda run, shell="auto", **kw: (run, True, shell))
    assert runner.tool_available(Job(id="a", gate="g", run="type report.txt", shell="cmd"), None)
    assert runner.tool_available(Job(id="b", gate="g", run="Get-Content x.txt", shell="powershell"), None)
    assert not runner.tool_available(Job(id="c", gate="g", run="typo-tool-xyz", shell="cmd"), None)


@needs_posix_shell
def test_python_tools_get_utf8_output(project):
    root = project(BASIC_PIPELINE, """\
        jobs:
          - {id: enc, gate: commit, run: "{PY} -c \\"import sys; print(sys.stdout.encoding, 'æøå')\\""}
    """)
    r = Engine(load_pipeline(root)).run(Selection(reports=["md"]))
    log = (Path(r.artifact_dir) / "logs" / "enc.log").read_text(encoding="utf-8")
    assert "utf-8 æøå" in log.lower()
