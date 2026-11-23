# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Runner edge cases found during the bug review (each test is a regression test)."""

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from gatewright import runner
from gatewright.models import ERROR, FAILED, PASSED, SKIPPED, Job
from gatewright.runner import JobHandle, execute
from gatewright.secrets import SecretStore

from .conftest import needs_posix_shell

from .conftest import PY  # noqa: E402

JUNIT_FAIL = """<testsuite><testcase classname="t" name="test_write">
<failure message="PermissionError: [Errno 13] Permission denied: '/etc/x'">Permission denied</failure>
</testcase></testsuite>"""


def run_job(tmp_path, job, **kw):
    (tmp_path / ".gatewright").mkdir(exist_ok=True)
    return execute(job, root=str(tmp_path), run_id="r", out_dir=str(tmp_path / "out"),
                   log_file=str(tmp_path / "job.log"), secrets=SecretStore(str(tmp_path)),
                   handle=kw.pop("handle", JobHandle(job)), **kw)


def _script(tmp_path, name, code):
    p = tmp_path / name
    p.write_text(code)
    return f'"{PY}" {name}'


def test_test_failure_mentioning_permission_denied_is_code_not_infra(tmp_path):
    """Regression: the words 'Permission denied' in a failing test made the job an INFRA error."""
    cmd = _script(tmp_path, "t.py", "import os, sys\nopen(os.environ['GATEWRIGHT_OUT'] + '/j.xml', 'w')"
                                    f".write({JUNIT_FAIL!r})\nprint('PermissionError: Permission denied')\nsys.exit(1)\n")
    r = run_job(tmp_path, Job(id="t", gate="g", run=cmd, parser={"type": "junit", "file": "$GATEWRIGHT_OUT/j.xml"}))
    assert r.status == FAILED and r.errors[0].category == "test_failure"
    assert "Permission denied" in r.errors[0].human


def test_pattern_infra_still_wins_when_results_are_empty(tmp_path):
    cmd = _script(tmp_path, "n.py", "import sys\nprint('curl: Could not resolve host: x')\nsys.exit(6)\n")
    r = run_job(tmp_path, Job(id="n", gate="g", run=cmd, parser={"type": "junit", "file": "missing.xml"}))
    assert r.status == ERROR and r.errors[0].category == "infra"


def test_stale_report_file_is_ignored(tmp_path):
    """Regression: a report left by an earlier run was parsed when the tool crashed before writing one."""
    rep = tmp_path / "reports"
    rep.mkdir()
    (rep / "junit.xml").write_text('<testsuite><testcase classname="a" name="ok"/></testsuite>')
    old = time.time() - 3600
    os.utime(rep / "junit.xml", (old, old))
    cmd = _script(tmp_path, "crash.py", "import sys\nsys.exit('tool crashed before writing a report')\n")
    r = run_job(tmp_path, Job(id="c", gate="g", run=cmd, parser={"type": "junit", "file": "reports/junit.xml"}))
    assert r.status == ERROR and "could not read its results" in r.errors[0].human
    assert "tests.total" not in r.metrics


def test_kill_requested_before_start_stops_the_job(tmp_path):
    """Regression: a stop request that arrived before the process existed was ignored."""
    job = Job(id="slow", gate="g", run=_script(tmp_path, "s.py", "import time\ntime.sleep(30)\n"), timeout=60)
    handle = JobHandle(job)
    handle.kill("system memory reached 99%")
    t0 = time.monotonic()
    r = run_job(tmp_path, job, handle=handle)
    assert time.monotonic() - t0 < 15
    assert r.status == ERROR and r.errors[0].category == "resource"


def test_cancel_interrupts_the_retry_wait(tmp_path):
    job = Job(id="net", gate="g", retries=3,
              run=_script(tmp_path, "net.py", "import sys\nprint('Connection refused')\nsys.exit(7)\n"))
    handle = JobHandle(job)
    threading.Timer(1.5, handle.kill, args=("run cancelled by user",)).start()
    t0 = time.monotonic()
    r = run_job(tmp_path, job, handle=handle)
    assert time.monotonic() - t0 < 6                      # waits would total 2+4+8 s without the fix
    assert r.status == SKIPPED and r.note == "cancelled" and r.attempts <= 2


def test_hung_docker_kill_does_not_raise(monkeypatch):
    def hang(*a, **kw):
        raise subprocess.TimeoutExpired(a[0], 15)
    monkeypatch.setattr(runner.shutil, "which", lambda name, path=None: "/usr/bin/docker")
    monkeypatch.setattr(runner.subprocess, "run", hang)
    runner._terminate(None, "gatewright-x")               # regression: TimeoutExpired escaped


def test_output_beyond_capture_limit_is_kept_in_the_log_only(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "MAX_CAPTURE", 1000)
    cmd = _script(tmp_path, "big.py", "for i in range(500):\n    print('line', i)\nraise SystemExit(1)\n")
    r = run_job(tmp_path, Job(id="big", gate="g", run=cmd))
    log = (tmp_path / "job.log").read_text(encoding="utf-8")
    assert "line 499" in log and r.status == FAILED
    assert "not kept in memory" in r.errors[0].raw or "line 499" not in r.errors[0].raw


def test_job_that_cannot_start(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise OSError("exec format error")
    monkeypatch.setattr(runner.subprocess, "Popen", boom)
    r = run_job(tmp_path, Job(id="x", gate="g", run="echo hi", parser={"type": "junit", "file": "x.xml"}))
    assert r.status == ERROR and "could not start the job" in r.errors[0].human   # regression: NameError


def test_tool_version_cache_expires(monkeypatch, tmp_path):
    calls = []
    real = runner.subprocess.run

    def counting(*a, **kw):
        calls.append(a[0])
        return real(*a, **kw)
    monkeypatch.setattr(runner.subprocess, "run", counting)
    runner.tool_version.cache_clear()
    runner.tool_version("git")
    runner.tool_version("git")
    assert len(calls) == 1                                  # cached
    monkeypatch.setattr(runner, "_TTL", 0.0)
    runner.tool_version("git")
    assert len(calls) == 2                                  # expired -> asked again (regression: never)
    assert runner.tool_version("definitely-not-a-tool-xyz") == ""


def test_when_exists_optional_and_missing_secret_paths(tmp_path):
    r = run_job(tmp_path, Job(id="w", gate="g", run="exit 1", when_exists=["nope.txt"]))
    assert r.status == SKIPPED
    r = run_job(tmp_path, Job(id="o", gate="g", run="not-installed-tool-xyz", optional=True))
    assert r.status == SKIPPED and "not installed" in r.note
    off = SecretStore(str(tmp_path), enabled=False)
    job = Job(id="s", gate="g", run="echo hi", secrets=["TOKEN"])
    r = execute(job, root=str(tmp_path), run_id="r", out_dir=str(tmp_path / "o2"), log_file=str(tmp_path / "l2"),
                secrets=off, handle=JobHandle(job))
    assert r.status == ERROR and "withheld" in r.errors[0].human


@needs_posix_shell
def test_warn_and_suppressed_statuses(tmp_path):
    ruff_json = ('[{"code": "E501", "message": "long", "filename": "a.py", "location": {"row": 1}},'
                 ' {"code": "F401", "message": "unused", "filename": "a.py", "location": {"row": 1}}]')
    (tmp_path / "a.py").write_text("import os\n")
    (tmp_path / "out.json").write_text(ruff_json)
    job = Job(id="lint", gate="g", run="cat out.json; exit 1", parser={"type": "ruff"}, fail_on="high")
    r = run_job(tmp_path, job)
    assert r.status == "warn" and "below fail_on=high" in r.note
    r = run_job(tmp_path, job, suppress=lambda e: setattr(e, "suppressed", True))
    assert r.status == PASSED and "suppressed by baseline" in r.note


def test_python_for_jobs_reports_source(tmp_path):
    exe, src = runner.python_for_jobs(str(tmp_path))
    assert exe and src in ("PATH", "Gatewright's environment (project has no .venv)")
    assert runner.project_venv(None) is None


@pytest.mark.parametrize("shell, os_name, expect", [("sh", "posix", "sh"), ("cmd", "posix", "sh"),
                                                    ("cmd", "nt", "cmd"), ("bash", "posix", "bash")])
def test_shell_command_variants(shell, os_name, expect):
    assert runner.shell_command("echo", shell, os_name=os_name, which=lambda n: f"/bin/{n}")[2] == expect


def test_unknown_shell_is_rejected():
    with pytest.raises(ValueError):
        runner.shell_command("echo", "fish")


def test_describe_command():
    assert runner.describe_command(Job(id="a", gate="g", run="ls -l")) == "ls -l"
    assert runner.describe_command(Job(id="a", gate="g", run="ls", image="alpine")).startswith("docker run alpine")


def test_tool_available_for_containers_and_builtins(monkeypatch):
    assert runner.tool_available(Job(id="a", gate="g", run="FOO=1 make"), None)
    monkeypatch.setattr(runner.shutil, "which", lambda *a, **k: None)
    assert not runner.tool_available(Job(id="a", gate="g", run="x", image="alpine"), None)


def test_has_module_handles_broken_python():
    runner._has_module.cache_clear()
    assert runner._has_module("/nonexistent/python", "json") is False


def test_kill_tree_survives_already_dead_process():
    p = subprocess.Popen([PY, "-c", "pass"])
    p.wait()
    runner._kill_tree(p)                                    # must not raise
    runner._terminate(p, None)


@pytest.mark.parametrize("run, expect", [('"/opt/my tools/py" x.py', "/opt/my tools/py"),
                                         ('"C:\\Program Files\\T\\t.exe" -v', "C:\\Program Files\\T\\t.exe"),
                                         ("'unterminated x", "'unterminated"), ("", "")])
def test_tool_name_handles_quoted_paths(run, expect):
    """Regression: a quoted program path kept its quotes and was reported as 'not found'."""
    assert Job(id="a", gate="g", run=run).tool_name == expect


def test_secrets_are_masked_in_report_files_and_outputs(tmp_path, monkeypatch):
    """Regression (security): a token inside a JUnit report reached history, reports and the artifact."""
    monkeypatch.setenv("GATEWRIGHT_SECRET_API_TOKEN", "s3cr3t-value-123")
    junit = ('<testsuite><testcase classname="t" name="test_api">'
             '<failure message="401 for token s3cr3t-value-123">auth failed</failure></testcase></testsuite>')
    cmd = _script(tmp_path, "leak.py", "import os, sys\nout = os.environ['GATEWRIGHT_OUT']\n"
                                       f"open(out + '/j.xml', 'w').write({junit!r})\n"
                                       "open(out + '/bin.dat', 'wb').write(b'\\xff\\xfe s3cr3t-value-123')\n"
                                       "sys.exit(1)\n")
    job = Job(id="api", gate="g", run=cmd, secrets=["API_TOKEN"], parser={"type": "junit", "file": "$GATEWRIGHT_OUT/j.xml"})
    r = run_job(tmp_path, job)
    assert r.status == FAILED
    assert "s3cr3t-value-123" not in r.errors[0].human + r.errors[0].raw and "***" in r.errors[0].human
    out = tmp_path / "out"
    assert "s3cr3t-value-123" not in (out / "j.xml").read_text(encoding="utf-8")
    assert (out / "bin.dat").read_bytes().startswith(b"\xff\xfe")       # binary files are left alone


def test_mask_output_files_without_secrets_is_a_no_op(tmp_path):
    (tmp_path / "a.txt").write_text("nothing secret")
    assert runner.mask_output_files(str(tmp_path), SecretStore(str(tmp_path))) == 0


def test_container_name_keeps_the_attempt_number(tmp_path):
    """Regression: a long job id cut off '-<attempt>', so a retry reused the container name."""
    job = Job(id="x" * 80, gate="g", run="true", image="alpine")
    names = {runner._docker_argv(job, str(tmp_path), "20261122-203045-4b86", str(tmp_path / "o"), [], a)[1]
             for a in (1, 2)}
    assert len(names) == 2 and all(len(n) <= 63 and n.endswith(("-1", "-2")) for n in names)


def test_secrets_are_masked_in_copied_outputs(project, monkeypatch):
    """Regression (security): files copied by `outputs:` reached the artifact unmasked."""
    from gatewright.config import load_pipeline
    from gatewright.engine import Engine, Selection
    from .conftest import BASIC_PIPELINE
    monkeypatch.setenv("GATEWRIGHT_SECRET_API_TOKEN", "tok-9876543210")
    root = project(BASIC_PIPELINE, f"""\
jobs:
  - id: build
    gate: commit
    run: '{PY} build.py'
    secrets: [API_TOKEN]
    outputs: [dist/*.txt]
""", files={"build.py": "import os, pathlib\npathlib.Path('dist').mkdir(exist_ok=True)\n"
                        "pathlib.Path('dist/config.txt').write_text('token=' + os.environ['API_TOKEN'])\n"})
    run = Engine(load_pipeline(root)).run(Selection(reports=["md"]))
    copied = Path(run.artifact_dir) / "out" / "build" / "dist" / "config.txt"
    assert copied.read_text(encoding="utf-8") == "token=***"
    from gatewright.artifacts import verify
    assert verify(Path(run.artifact_dir)) == []          # masked before the checksums were written
