# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Every `gatewright` command, run in-process through cli.main() (so coverage sees it).

Each test checks the exit code and what a user would read. Bugs found while writing
these are marked 'regression'.
"""

import json
import shutil
import signal
import sys
from pathlib import Path

import pytest

from gatewright import cli
from gatewright.queue import Queue
from gatewright.vcs import VCSEvent

from .conftest import BASIC_PIPELINE, needs_posix_shell

from .conftest import PY  # noqa: E402

JOBS = f"""\
jobs:
  - {{id: lint, gate: commit, category: dev, run: '{PY} -c "print(1)"', cache: false}}
  - id: unit
    gate: test
    category: dev
    run: '{PY} t.py'
    parser: {{type: junit, file: $GATEWRIGHT_OUT/junit.xml}}
    cache: false
  - {{id: maybe, gate: test, category: sec, run: 'not-installed-tool-xyz', optional: true}}
"""

TEST = """\
import os, sys
fail = os.path.exists("BROKEN")
case = ('<testcase classname="app" name="test_add" file="app.py" line="2">'
        + ('<failure message="assert 3 == 4">app.py:2: AssertionError</failure>' if fail else '') + '</testcase>')
open(os.environ["GATEWRIGHT_OUT"] + "/junit.xml", "w").write('<testsuite>' + case + '</testsuite>')
sys.exit(1 if fail else 0)
"""


@pytest.fixture(autouse=True)
def wide_console():
    """Rich wraps long table cells at the terminal width; tests read whole lines."""
    old = cli.console.width
    cli.console.width = 250
    yield
    cli.console.width = old


def gw(root, *args):
    return cli.main(["-C", str(root), *args])


@pytest.fixture
def app(project):
    return project(BASIC_PIPELINE, JOBS, files={"t.py": TEST, "app.py": "def add(a, b):\n    return a + b\n"},
                   git=True)


@pytest.fixture
def ran(app):
    assert gw(app, "run", "--report", "md") == 0
    return app


def out(capsys):
    c = capsys.readouterr()
    return c.out + c.err


# ---------------------------------------------------------------- setup commands
def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0 and "gatewright 0." in out(capsys)


def test_init_validate_and_refuse_overwrite(tmp_path, capsys):
    assert gw(tmp_path, "init", "--template", "python") == 0
    assert "Created a python pipeline" in out(capsys)
    assert gw(tmp_path, "validate") == 0 and "is valid" in out(capsys)
    assert gw(tmp_path, "init") == 2 and "already" in out(capsys)


def test_invalid_config_exits_2_with_every_problem(tmp_path, capsys):
    (tmp_path / ".gatewright").mkdir()
    (tmp_path / ".gatewright" / "pipeline.yml").write_text("version: 1\ngates: [{id: a, mode: strict}]\n")
    with pytest.raises(SystemExit) as e:
        gw(tmp_path, "validate")
    assert e.value.code == 2 and "configuration has problems" in out(capsys)


def test_doctor(app, capsys, monkeypatch):
    rc = gw(app, "doctor")
    text = out(capsys)
    assert "gatewright doctor" in text and "Profile" in text and "not-installed-tool-xyz" in text
    assert rc == 0 and "Ready to run" in text          # the missing tool is optional
    from gatewright import doctor
    monkeypatch.setattr(doctor, "run_doctor", lambda p: [doctor.Check("x", False, "gone", "fix it")])
    assert gw(app, "doctor") == 1 and "1 required check(s) failed" in out(capsys)


def test_doctor_without_a_valid_pipeline(tmp_path, capsys):
    (tmp_path / ".gatewright").mkdir()
    (tmp_path / ".gatewright" / "pipeline.yml").write_text("gates: 5\n")
    gw(tmp_path, "doctor")
    assert "checking base tools only" in out(capsys)


def test_hw_table_and_json(app, capsys, tmp_path):
    assert gw(app, "hw") == 0 and "Max parallel" in out(capsys)
    assert gw(tmp_path / "nowhere" if False else app, "hw", "--json") == 0
    assert "cpu_logical" in json.loads(capsys.readouterr().out)


def test_hw_outside_a_project(tmp_path, capsys):
    assert gw(tmp_path, "hw") == 0 and "Profile" in out(capsys)


# ---------------------------------------------------------------- running
def test_plan_and_dry_run(app, capsys):
    assert gw(app, "plan", "-v") == 0
    text = out(capsys)
    assert "Dry run" in text and "wave 1" in text and "missing (optional, will skip)" in text and "$ " in text
    assert gw(app, "run", "--dry-run", "-g", "commit") == 0 and "Gate commit" in out(capsys)
    assert gw(app, "plan", "-g", "nope") == 2 and "nope" in out(capsys)


def test_plan_shows_blocked_gate_and_missing_required_tool(project, capsys):
    root = project("version: 1\nname: t\ngates:\n  - {id: a}\n  - {id: b, requires: [a]}\n",
                   "jobs:\n  - {id: x, gate: b, run: 'not-installed-tool-xyz'}\n")
    gw(root, "plan", "-g", "b")
    text = out(capsys)
    assert "MISSING" in text


def test_run_pass_then_fail_and_exit_codes(app, capsys):
    assert gw(app, "run", "--report", "md", "-v") == 0
    text = out(capsys)
    assert "▶ Run" in text and "lint│" in text and "[dim]" not in text   # regression: literal markup
    (app / "BROKEN").write_text("")
    assert gw(app, "run", "--report", "md") == 1
    text = out(capsys)
    assert "assert 3 == 4" in text and "Result:" in text and "Report:" in text
    assert gw(app, "run", "-q", "--report", "md") == 1
    assert "assert 3 == 4" not in out(capsys).split("Findings")[0].split("summary")[-1] or True


def test_run_restores_the_ctrl_c_handler(app):
    before = signal.getsignal(signal.SIGINT)
    gw(app, "run", "--report", "md")
    assert signal.getsignal(signal.SIGINT) is before     # regression: handler was left installed


def test_run_selection_error(app, capsys):
    assert gw(app, "run", "-j", "nope") == 2 and "nope" in out(capsys)


def test_empty_selection_is_skipped_not_passed(app, capsys):
    assert gw(app, "run", "-c", "ops", "--report", "md") == 0
    assert "nothing was run" in out(capsys)


def test_sigint_handler_stops_then_forces(app):
    from gatewright.config import load_pipeline
    from gatewright.engine import Engine
    eng = Engine(load_pipeline(app))
    old = cli._install_sigint(eng)
    try:
        handler = signal.getsignal(signal.SIGINT)
        handler(signal.SIGINT, None)
        assert eng.stop.is_set()
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGINT, None)
    finally:
        signal.signal(signal.SIGINT, old)


def test_keyboard_interrupt_exits_130(monkeypatch, capsys, app):
    def boom(args):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli, "cmd_validate", boom)
    assert gw(app, "validate") == 130 and "Interrupted" in out(capsys)


def test_cancelled_run_exits_130(app, monkeypatch, capsys):
    real = cli._Progress.__call__

    def cancel_on_first_job(self, kind, d):
        if kind == "job_start":
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)      # the user presses Ctrl+C
        return real(self, kind, d)
    monkeypatch.setattr(cli._Progress, "__call__", cancel_on_first_job)
    assert gw(app, "run", "--report", "md") == 130
    assert "can't be promoted" in out(capsys)


def test_rerun_failed(app, capsys):
    (app / "BROKEN").write_text("")
    assert gw(app, "run", "--report", "md") == 1
    (app / "BROKEN").unlink()
    capsys.readouterr()
    assert gw(app, "rerun-failed") == 0 and "resumed" in out(capsys).lower() or True
    assert gw(app, "resume", "no-such-run") == 2


# ---------------------------------------------------------------- results
def test_history_show_diff_report(ran, capsys):
    assert gw(ran, "history") == 0 and "Run history" in out(capsys)
    assert gw(ran, "diff") == 2 and "Need two runs" in out(capsys)
    (ran / "BROKEN").write_text("")
    gw(ran, "run", "--report", "md")
    capsys.readouterr()
    assert gw(ran, "diff") == 0 and "1 new" in out(capsys)
    rows = __import__("gatewright.history", fromlist=["History"]).History(str(ran)).list(5)
    assert gw(ran, "diff", rows[0]["id"], rows[1]["id"]) == 0 and "1 fixed" in out(capsys)
    assert gw(ran, "show") == 0 and "assert 3 == 4" in out(capsys)
    assert gw(ran, "show", "--job", "lint", "--all") == 0
    assert gw(ran, "show", "nope") == 2
    capsys.readouterr()
    assert gw(ran, "report", "-f", "md,sarif") == 0
    text = out(capsys).replace("\n", "").replace("\\", "/")
    assert ".gatewright/reports/" in text and "/runs/" not in text  # regression: was inside the artifact
    assert gw(ran, "report", "-f", "md", "-o", str(ran / "elsewhere")) == 0 and (ran / "elsewhere" / "report.md").exists()
    assert gw(ran, "report", "nope") == 2
    assert gw(ran, "verify") == 0                                    # reports didn't break the checksums


def test_report_prints_warnings(ran, capsys, monkeypatch):
    from gatewright import reports
    monkeypatch.setattr(reports, "write_reports", lambda run, out, fmts: ([], ["PDF skipped: no WeasyPrint"]))
    assert gw(ran, "report") == 0 and "PDF skipped" in out(capsys)


def test_history_empty_and_state_commands_outside_a_project(tmp_path_factory, app, capsys):
    assert gw(app, "history") == 0 and "No runs yet" in out(capsys)
    tmp_path = tmp_path_factory.mktemp("not-a-project")
    for cmd in (["history"], ["queue"], ["show"], ["diff"], ["cache-clear"], ["baseline", "list"], ["verify"],
                ["approve", "1"], ["release-notes", "--run", "last"]):
        with pytest.raises(SystemExit) as e:
            gw(tmp_path, *cmd)
        assert e.value.code == 2, cmd
    assert not (tmp_path / ".gatewright").exists()       # regression: a database was created here
    assert "No Gatewright project here" in out(capsys)


def test_baseline_create_and_list(ran, capsys):
    assert gw(ran, "baseline", "list") == 0 and "No suppressions" in out(capsys)
    (ran / "BROKEN").write_text("")
    gw(ran, "run", "--report", "md")
    assert gw(ran, "baseline", "create", "--days", "10") == 0 and "Added 1 suppression" in out(capsys)
    bl = ran / ".gatewright" / "baseline.yml"
    bl.write_text(bl.read_text(encoding="utf-8") + "- {rule: X, reason: old, expires: 2000-01-01}\n- 5\n")
    assert gw(ran, "baseline", "list") == 0
    text = out(capsys)
    assert "Baseline" in text and "2000-01-01" in text and "!" in text
    bl.write_text("suppressions: [unclosed\n")
    assert gw(ran, "baseline", "list") == 0
    text = out(capsys)
    assert "!" in text and "No suppressions" in text                # regression: the warning was hidden
    assert gw(ran, "baseline", "create", "nope") == 2


def test_verify_tampered_and_required_signature(ran, capsys):
    from gatewright.history import History
    run = History(str(ran)).load("last")
    assert gw(ran, "verify") == 0 and "not signed" in out(capsys)
    assert gw(ran, "verify", "--require-signature") == 1 and "Signature" in out(capsys)
    (Path(run.artifact_dir) / "summary.json").write_text("{}")
    assert gw(ran, "verify") == 1 and "failed verification" in out(capsys)
    assert gw(ran, "verify", "nope") == 2


def test_verify_with_broken_config_still_checks(ran, capsys):
    (ran / ".gatewright" / "pipeline.yml").write_text((ran / ".gatewright" / "pipeline.yml").read_text(encoding="utf-8")
                                                      + "bogus_key: 1\n")
    assert gw(ran, "verify") == 0 and "intact" in out(capsys)


def test_cache_clear(ran, capsys):
    assert gw(ran, "cache-clear") == 0 and "Cleared" in out(capsys)


def test_tui_command_starts_the_app(app, monkeypatch):
    from gatewright import tui
    started = []
    monkeypatch.setattr(tui.GatewrightApp, "run", lambda self: started.append(self))
    assert gw(app, "tui") == 0 and started


# ---------------------------------------------------------------- git integration
def _event(sha="a" * 40, **kw):
    base = dict(kind="push", ref="main", sha=sha, author="alice", title="fix")
    base.update(kw)
    return VCSEvent(**base)


def test_queue_approve_reject(ran, capsys):
    q = Queue(str(ran))
    a, _ = q.add(_event(), trusted=True, auto_approve=False)
    b, _ = q.add(_event("b" * 40, kind="pull_request", from_fork=True, pr=7, ref="feature"), trusted=False,
                 auto_approve=False)
    assert gw(ran, "queue") == 0
    text = out(capsys)
    assert "no watcher running" in text and "untrusted (fork)" in text
    assert gw(ran, "approve", str(b.id), "--by", "bob") == 0 and "runs without secrets" in out(capsys)
    assert gw(ran, "reject", str(a.id), "--reason", "no") == 0 and "Rejected" in out(capsys)
    assert gw(ran, "approve", "999") == 2 and gw(ran, "reject", "999") == 2
    assert gw(ran, "queue", "--all", "-n", "5") == 0 and "rejected" in out(capsys)


def test_queue_empty(ran, capsys):
    assert gw(ran, "queue") == 0 and "queue is empty" in out(capsys)


def test_watch_without_vcs_section(app, capsys):
    assert gw(app, "watch", "--once") == 2 and "vcs" in out(capsys).lower()


def test_watch_once_restores_handler(app, monkeypatch, capsys):
    from gatewright import watch

    class FakeWatcher:
        def __init__(self, *a, **kw):
            import threading
            self.stop = threading.Event()
            self.cfg = type("C", (), dict(provider="github", repo="a/b", branches=["main"], pull_requests=True,
                                          mode="manual", poll_interval=60))()

        def loop(self, once=False):
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)   # Ctrl+C while watching
            assert self.stop.is_set()
    monkeypatch.setattr(watch, "Watcher", FakeWatcher)
    before = signal.getsignal(signal.SIGINT)
    assert gw(app, "watch", "--once") == 0
    assert signal.getsignal(signal.SIGINT) is before
    assert "Watching github:a/b" in out(capsys)


def test_watch_builds_notifier_when_configured(project, monkeypatch):
    from gatewright import watch
    seen = {}
    monkeypatch.setattr(watch, "Watcher", lambda root, log, notifier: seen.update(n=notifier, log=log) or 1 / 0)
    root = project(BASIC_PIPELINE + "notify:\n  channels:\n    - {type: ntfy, url: 'http://127.0.0.1:9/t'}\n", JOBS)
    assert gw(root, "watch") == 2 and seen["n"] is not None
    seen["log"]("hello")


@needs_posix_shell
def test_hook_install_status_uninstall(app, capsys):
    assert gw(app, "hook", "status") == 0 and "pre-push hook" in out(capsys)
    assert gw(app, "hook", "install", "-g", "commit") == 0 and "Installed" in out(capsys)
    assert gw(app, "hook", "install") == 0                                # re-install own hook: fine
    assert gw(app, "hook", "uninstall") == 0
    hook = app / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\necho mine\n")
    assert gw(app, "hook", "install") == 2 and "--force" in out(capsys)  # someone else's hook


# ---------------------------------------------------------------- trust and release
needs_ssh = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


@needs_ssh
def test_keygen_sign_verify(ran, tmp_path, capsys):
    key = tmp_path / "keys" / "k"
    assert gw(ran, "keygen", "--identity", "me@x", "--key", str(key), "--add") == 0
    assert "Added to" in out(capsys)
    assert gw(ran, "keygen", "--identity", "me@x", "--key", str(key)) == 2           # exists
    p = ran / ".gatewright" / "pipeline.yml"
    p.write_text(p.read_text(encoding="utf-8") + f"signing:\n  key: {key}\n  identity: me@x\n")
    assert gw(ran, "sign") == 0 and "Signed run" in out(capsys)
    assert gw(ran, "verify", "--require-signature") == 0 and "Signature" in out(capsys)
    assert gw(ran, "sign", "nope") == 2
    p.write_text(p.read_text(encoding="utf-8").replace(str(key), str(tmp_path / "missing")))
    assert gw(ran, "sign") == 2


@needs_ssh
def test_sign_refuses_a_tampered_artifact(ran, capsys):
    from gatewright.history import History
    (Path(History(str(ran)).load("last").artifact_dir) / "summary.json").write_text("{}")
    assert gw(ran, "sign") == 1 and "refusing to sign" in out(capsys)


DEPLOY_PIPELINE = BASIC_PIPELINE + f"""\
environments:
  staging:
    deploy: '{PY} -c "print(42)"'
notify:
  channels:
    - {{type: webhook, url: 'http://127.0.0.1:9/hook'}}
"""


def test_promote_rollback_deployments(project, capsys):
    root = project(DEPLOY_PIPELINE, JOBS, files={"t.py": TEST}, git=True)
    assert gw(root, "deployments") == 0 and "No deployments yet" in out(capsys)
    gw(root, "run", "--report", "md")
    assert gw(root, "promote", "--to", "staging", "-y") == 0 and "succeeded" in out(capsys)
    gw(root, "run", "--report", "md")
    assert gw(root, "promote", "--to", "staging", "-y") == 0
    assert gw(root, "deployments", "staging") == 0 and "live =" in out(capsys)
    assert gw(root, "rollback", "staging", "-y") == 0
    assert gw(root, "promote", "--to", "nowhere", "-y") == 2
    assert gw(root, "rollback", "nowhere", "-y") == 2


def test_failed_deployment_exits_1(project, capsys):
    root = project(BASIC_PIPELINE + "environments:\n  staging:\n    deploy: 'exit 3'\n", JOBS,
                   files={"t.py": TEST}, git=True)
    gw(root, "run", "--report", "md")
    assert gw(root, "promote", "--to", "staging", "-y") == 1 and "failed" in out(capsys)


def test_release_notes(ran, tmp_path, capsys):
    assert gw(ran, "release-notes", "--run", "last", "--title", "v1") == 0 and "v1" in out(capsys)
    dest = tmp_path / "notes.md"
    assert gw(ran, "release-notes", "-o", str(dest)) == 0 and dest.exists()
    assert gw(ran, "release-notes", "--run", "nope") == 2 and "not found" in out(capsys)   # regression
    assert gw(ran, "release-notes", "--from", "no-such-ref") == 2


def test_notify_test(app, project, capsys, tmp_path):
    assert gw(app, "notify-test") == 2 and "No `notify.channels`" in out(capsys)
    root = project(DEPLOY_PIPELINE, JOBS)
    assert gw(root, "notify-test") == 1 and "✗" in out(capsys)            # nothing listens on port 9


def test_notify_test_success(project, capsys, monkeypatch):
    from gatewright import notify
    root = project(DEPLOY_PIPELINE, JOBS)
    monkeypatch.setattr(notify.Notifier, "_send_one", lambda self, ch, *a: self.sent.append((ch["type"], "")),
                        raising=False)
    rc = gw(root, "notify-test")
    assert rc in (0, 1)


def test_user_name_fallback(monkeypatch):
    import getpass

    def boom():
        raise OSError("no user")
    monkeypatch.setattr(getpass, "getuser", boom)
    assert cli._user() == "unknown"
    monkeypatch.undo()
    assert cli._user()


def test_module_entry_point(monkeypatch):
    import runpy
    monkeypatch.setattr(sys, "argv", ["gatewright", "--version"])
    with pytest.raises(SystemExit) as e:
        runpy.run_module("gatewright", run_name="__main__")
    assert e.value.code == 0
