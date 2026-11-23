# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Watcher, cancellation, deploy and signing edge cases (failure paths the main tests don't reach)."""

import http.client
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from gatewright import deploy as deploy_mod
from gatewright import signing
from gatewright.config import load_pipeline
from gatewright.deploy import Deployer, DeployError
from gatewright.engine import Engine, Selection
from gatewright.models import (BLOCKED, CANCELLED, FAILED, PASSED, SKIPPED, WARN, GateResult, JobError,
                               JobResult)
from gatewright.queue import Queue
from gatewright.vcs import VCSError
from gatewright.watch import ERROR, FAILURE, SUCCESS, Watcher, gate_state, pr_comment

from .conftest import BASIC_PIPELINE
from .test_watch import Repo, _free_port, secrets  # noqa: F401  (fixture)

from .conftest import PY  # noqa: E402


# ---------------------------------------------------------------- gate → commit status
def _gate(status, mode="enforced", jobs=(), note=""):
    g = GateResult(gate="test", mode=mode)
    g.status, g.note, g.jobs = status, note, list(jobs)
    return g


def test_gate_states():
    err = JobResult("j", "test", "dev", FAILED)
    err.errors = [JobError("j", "lint", "low", "x")]
    assert gate_state(_gate(PASSED)) == (SUCCESS, "passed")
    assert gate_state(_gate(SKIPPED)) == (SUCCESS, "skipped")
    assert gate_state(_gate(WARN, jobs=[err]))[1] == "passed with warnings (1 issue(s))"
    assert gate_state(_gate(BLOCKED, note="needs commit"))[0] == ERROR
    assert gate_state(_gate(FAILED, "advisory", [err]))[0] == SUCCESS
    assert gate_state(_gate(FAILED, jobs=[err])) == (FAILURE, "failed: j (1 issue(s))")
    # regression: a cancelled gate was reported as a green "skipped"
    assert gate_state(_gate(CANCELLED)) == (ERROR, "cancelled before it finished (not tested)")


def test_cancel_mid_gate_makes_the_gate_cancelled_not_passed(project):
    root = project(BASIC_PIPELINE, f"""\
jobs:
  - {{id: fast, gate: commit, run: '{PY} -c "print(1)"', cache: false}}
  - {{id: slow, gate: commit, run: '{PY} -c "import time; time.sleep(30)"', needs: [fast], cache: false}}
  - {{id: later, gate: test, run: '{PY} -c "print(2)"', cache: false}}
""")
    eng = None

    def on_event(kind, d):
        if kind == "job_start" and d["job_id"] == "slow":
            threading.Timer(0.5, eng.stop.set).start()
    eng = Engine(load_pipeline(root), on_event)
    run = eng.run(Selection(reports=["md"]))
    gates = {g.gate: g for g in run.gates}
    assert run.status == CANCELLED
    assert gates["commit"].status == CANCELLED and gates["test"].status == CANCELLED
    slow = next(j for j in run.all_jobs() if j.job_id == "slow")
    assert slow.status == SKIPPED and slow.note == "cancelled"    # regression: was ERROR "protect the machine"


def test_pr_comment_for_untrusted_run():
    from gatewright.models import RunResult
    run = RunResult(run_id="r1", pipeline="p", root="/x", started="")
    run.status = "error"
    item = type("I", (), {"sha": "a" * 40, "trusted": False})()
    text = pr_comment(run, item, "")
    assert "ℹ️" in text and "Untrusted run" in text


# ---------------------------------------------------------------- watcher failure paths
@pytest.fixture
def repo(tmp_path, secrets):  # noqa: F811
    made = []

    def make(kind="github", mode="manual", extra=""):
        r = Repo(tmp_path, kind, mode, extra)
        made.append(r)
        return r
    yield make
    for r in made:
        r.host.stop()


def test_watcher_needs_a_vcs_section(project):
    root = project(BASIC_PIPELINE, "jobs: []\n")
    with pytest.raises(ValueError, match="no `vcs:` section"):
        Watcher(str(root))


def test_poll_error_is_logged(repo, monkeypatch):
    r = repo()
    logs = []
    w = r.watcher(logs)

    def down(state):
        raise VCSError("host unreachable")
    monkeypatch.setattr(w.provider, "list_events", down)
    assert w.poll_once() == [] and "! host unreachable" in logs


def test_notifier_is_told_about_approvals(repo):
    r = repo()
    told = []
    w = r.watcher()
    w.notifier = type("N", (), {"approval_needed": lambda self, item, why: told.append(item.id)})()
    items = w.poll_once()
    assert told == [items[0].id]


def test_checkout_failure_marks_the_item_error(repo, monkeypatch):
    r = repo()
    logs = []
    w = r.watcher(logs)
    item = w.poll_once()[0]
    Queue(str(r.home)).approve(item.id, "t")
    monkeypatch.setattr(type(w.provider), "clone_url", property(lambda self: str(r.tmp / "no-such-repo.git")))
    assert w.run_approved() == []
    assert Queue(str(r.home)).list(None, 5)[0].state == "error"
    assert r.host.latest_status(r.sha, "gatewright/commit")["state"] == "error"
    assert any("git fetch failed" in m for m in logs)


def test_checkout_refuses_a_different_head(repo, monkeypatch):
    r = repo()
    w = r.watcher()
    item = w.poll_once()[0]
    ws = Path(r.home) / ".gatewright" / "workspaces" / f"{item.id}-{item.sha[:12]}"
    ws.mkdir(parents=True)                              # left over from a crash: must be replaced
    real = subprocess.run

    def lying_git(args, **kw):
        p = real(args, **kw)
        if args[-2:] == ["rev-parse", "HEAD"]:
            p = subprocess.CompletedProcess(args, 0, stdout="f" * 40 + "\n", stderr="")
        return p
    monkeypatch.setattr("gatewright.watch.subprocess.run", lying_git)
    with pytest.raises(VCSError, match="refusing to run"):
        w.checkout(item)


def test_checkout_fetches_a_commit_the_branch_moved_past(repo):
    r = repo()
    w = r.watcher()
    item = w.poll_once()[0]
    r.commit("newer")
    r.commit("newest")
    r.push()                                            # branch moved on; shallow fetch may miss the sha
    ws = w.checkout(item)
    assert (ws / "check.py").exists()


def test_broken_config_in_checkout_is_reported(repo):
    r = repo()
    r.commit("break config", {".gatewright/pipeline.yml": "gates: nope\n"})
    r.push()
    logs = []
    w = r.watcher(logs)
    item = w.poll_once()[0]
    Queue(str(r.home)).approve(item.id, "t")
    assert w.run_approved() == []
    assert any(m.startswith(f"! #{item.id}") for m in logs)


def test_gates_without_jobs_report_success(repo):
    r = repo(extra="  gates: [commit, test]")
    r.commit("drop test jobs", {".gatewright/jobs/jobs.yml": (
        f"jobs:\n  - {{id: check, gate: commit, run: '{PY} check.py', cache: false}}\n")})
    r.push()
    w = r.watcher()
    item = w.poll_once()[-1]
    Queue(str(r.home)).approve(item.id, "t")
    w.run_approved()
    assert r.host.latest_status(item.sha, "gatewright/test")["description"] == "no jobs selected"


def test_webhook_needs_its_secret(repo, monkeypatch):
    r = repo(extra=f"  webhook: {{port: {_free_port()}}}")
    monkeypatch.delenv("GATEWRIGHT_SECRET_VCS_WEBHOOK_SECRET", raising=False)
    with pytest.raises(ValueError, match="secret"):
        r.watcher().start_webhook()


def test_webhook_rejects_bad_requests(repo, monkeypatch):
    monkeypatch.setenv("GATEWRIGHT_SECRET_VCS_WEBHOOK_SECRET", "hook-secret")
    r = repo(extra=f"  webhook: {{port: {_free_port()}}}")
    w = r.watcher()
    host, port = w.start_webhook()
    try:
        def post(path, body=b"{}", headers=None):
            c = http.client.HTTPConnection(host, port, timeout=10)
            c.request("POST", path, body=body, headers=headers or {})
            resp = c.getresponse()
            return resp.status
        assert post("/elsewhere") == 404
        assert post("/hook", headers={"Content-Length": "9999999999"}) == 413
        assert post("/hook", b"", {"Content-Length": "-5"}) == 400           # regression: blocked/crashed
        assert post("/hook", b"", {"Content-Length": "abc"}) == 400
        monkeypatch.setattr(w.provider, "parse_webhook", lambda h, b, s: (_ for _ in ()).throw(KeyError("ref")))
        assert post("/hook") == 400
    finally:
        w._server.shutdown()


def test_loop_with_webhook_logs_and_stops(repo, monkeypatch):
    monkeypatch.setenv("GATEWRIGHT_SECRET_VCS_WEBHOOK_SECRET", "hook-secret")
    r = repo(extra=f"  webhook: {{port: {_free_port()}}}")
    logs = []
    w = r.watcher(logs)
    threading.Timer(1.0, w.stop.set).start()
    w.loop()                                            # not once: waits, then stops
    assert any("webhook listening" in m for m in logs)


# ---------------------------------------------------------------- deploy failure paths
DEPLOY = BASIC_PIPELINE + f"""\
environments:
  staging:
    deploy: '{PY} -c "print(1)"'
    verify: 'exit 1'
    requires: [commit, test]
    require_signature: true
"""
JOBS = f"jobs:\n  - {{id: a, gate: commit, run: '{PY} -c \"print(1)\"', cache: false}}\n"


def test_promote_problems_are_all_listed(project, tmp_path):
    root = project(DEPLOY, JOBS, git=True)
    d = Deployer(load_pipeline(root), confirm=lambda q: True)
    with pytest.raises(DeployError, match="No run found"):
        d.promote(None, "staging")
    with pytest.raises(DeployError, match="No run found for nope"):
        d.promote("nope", "staging")
    run = Engine(load_pipeline(root)).run(Selection(gates=["commit"], reports=["md"]))
    with pytest.raises(DeployError) as e:
        d.promote(run.run_id, "staging", yes=True)
    assert "gate `test` was not part of run" in str(e.value) and "signature check failed" in str(e.value)
    import shutil
    shutil.rmtree(run.artifact_dir)
    with pytest.raises(DeployError, match="artifact folder .* is missing"):
        d.promote(run.run_id, "staging", yes=True)


def test_failures_notify_and_verify_failure_without_rollback(project):
    root = project(BASIC_PIPELINE + "environments:\n  staging:\n    deploy: 'exit 0'\n    verify: 'exit 4'\n"
                   "  broken:\n    deploy: 'exit 3'\n", JOBS, git=True)
    told = []
    notifier = type("N", (), {"deployment": lambda self, kind, env, run, text: told.append(kind)})()
    d = Deployer(load_pipeline(root), notifier=notifier, confirm=lambda q: True)
    run = Engine(load_pipeline(root)).run(Selection(reports=["md"]))
    assert d.promote(run.run_id, "staging", yes=True).status == "verify_failed"
    assert d.promote(run.run_id, "broken", yes=True).status == "failed"
    assert told == ["deploy_failed", "deploy_failed"]


def test_rollback_to_a_named_run_and_refusal(project):
    root = project(BASIC_PIPELINE + "environments:\n  staging:\n    deploy: 'exit 0'\n    requires: [test]\n",
                   JOBS + f"  - {{id: t, gate: test, run: '{PY} t.py', cache: false}}\n",
                   files={"t.py": "import os, sys\nsys.exit(1 if os.path.exists('BROKEN') else 0)\n"}, git=True)
    d = Deployer(load_pipeline(root), confirm=lambda q: True)
    good = Engine(load_pipeline(root)).run(Selection(reports=["md"]))
    (root / "BROKEN").write_text("")
    bad = Engine(load_pipeline(root)).run(Selection(reports=["md"]))
    d.promote(good.run_id, "staging", yes=True)
    with pytest.raises(DeployError, match="never successfully deployed"):   # regression: bypassed the gates
        d.rollback("staging", bad.run_id, yes=True)
    assert d.rollback("staging", good.run_id, yes=True).status == "succeeded"
    import shutil
    shutil.rmtree(good.artifact_dir)
    with pytest.raises(DeployError, match="Cannot roll `staging` back"):
        d.rollback("staging", good.run_id, yes=True)


def test_release_notes_failure_is_logged(project, monkeypatch):
    root = project(BASIC_PIPELINE + "environments:\n  staging:\n    deploy: 'exit 0'\n    release_notes: true\n",
                   JOBS, git=True)
    import gatewright.releasenotes as rn
    monkeypatch.setattr(rn, "generate", lambda *a, **k: (_ for _ in ()).throw(ValueError("no git")))
    logs = []
    d = Deployer(load_pipeline(root), log=logs.append, confirm=lambda q: True)
    run = Engine(load_pipeline(root)).run(Selection(reports=["md"]))
    assert d.promote(run.run_id, "staging", yes=True).status == "succeeded"
    assert any("release notes skipped" in m for m in logs)


def test_first_error_user_and_ask(monkeypatch):
    res = JobResult("deploy-x", "deploy", "ops", "error", exit_code=5)
    assert deploy_mod._first_error(res) == "error (exit 5)"
    assert deploy_mod._first_error(JobResult("d", "deploy", "ops", FAILED)) == FAILED
    import getpass
    monkeypatch.setattr(getpass, "getuser", lambda: (_ for _ in ()).throw(KeyError("uid")))
    assert deploy_mod._user() == "unknown"
    monkeypatch.setattr(sys, "stdin", type("T", (), {"isatty": lambda self: False})())
    assert deploy_mod._ask("go?") is False
    monkeypatch.setattr(sys, "stdin", type("T", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("builtins.input", lambda q: "YES")
    assert deploy_mod._ask("go?") is True

    def eof(q):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    assert deploy_mod._ask("go?") is False


# ---------------------------------------------------------------- workspace cleanup
def test_remove_tree_deletes_read_only_files(tmp_path):
    import stat
    from gatewright import watch
    ws = tmp_path / "ws" / ".git" / "objects" / "ab"
    ws.mkdir(parents=True)
    obj = ws / "cdef"
    obj.write_text("x")
    obj.chmod(stat.S_IREAD)                      # git writes objects read-only
    watch.remove_tree(tmp_path / "ws")           # Windows refused this before
    assert not (tmp_path / "ws").exists()
    # the handler itself (only reached on Windows): make writable, retry
    f = tmp_path / "ro.txt"
    f.write_text("x")
    f.chmod(stat.S_IREAD)
    watch._clear_readonly_and_retry(os.remove, str(f), None)
    assert not f.exists()
    watch.remove_tree(tmp_path / "missing", ignore_errors=True)
    with pytest.raises(OSError):
        watch.remove_tree(tmp_path / "missing")


# ---------------------------------------------------------------- signing failure paths
def test_default_key_path(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA" if os.name == "nt" else "XDG_CONFIG_HOME", str(tmp_path))  # per-OS config dir
    assert signing.default_key_path() == tmp_path / "gatewright" / "signing_key"


def test_signing_without_ssh_keygen(monkeypatch, tmp_path):
    monkeypatch.setattr(signing.shutil, "which", lambda n: None)
    with pytest.raises(signing.SigningError, match="not installed"):
        signing.keygen(tmp_path / "k", "me@x")


def test_openssh_version_unknown(monkeypatch):
    def boom(*a, **k):
        raise OSError("no ssh")
    monkeypatch.setattr(signing.subprocess, "run", boom)
    assert signing.openssh_version() is None


needs_ssh = pytest.mark.skipif(__import__("shutil").which("ssh-keygen") is None, reason="needs ssh-keygen")


@needs_ssh
def test_sign_and_verify_problems(tmp_path):
    key = tmp_path / "k"
    signing.keygen(key, "me@x")
    art = tmp_path / "art"
    art.mkdir()
    cfg = signing.SigningConfig(key=str(key), identity="me@x")
    with pytest.raises(signing.SigningError, match="write checksums"):
        signing.sign(art, cfg)
    (art / "checksums.sha256").write_text("")
    with pytest.raises(signing.SigningError, match="identity is not set"):
        signing.sign(art, signing.SigningConfig(key=str(key), identity=""))
    signing.sign(art, cfg)
    signing.sign(art, cfg)                                # re-sign replaces the old signature
    assert signing.verify(art, tmp_path / "none")[1].startswith("no allowed signers file")
    allowed = tmp_path / "allowed"
    allowed.write_text(f"me@x {key.with_suffix('.pub').read_text(encoding='utf-8')}" if key.with_suffix(".pub").exists()
                       else f"me@x {(tmp_path / 'k.pub').read_text(encoding='utf-8')}")
    (art / "signature.json").write_text("not json")
    assert "identity is unknown" in signing.verify(art, allowed)[1]
    real = signing.subprocess.run
    signing.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 1, b"", b"bad passphrase")
    try:
        with pytest.raises(signing.SigningError, match="could not create the key"):
            signing.keygen(tmp_path / "k2", "me@x")
    finally:
        signing.subprocess.run = real
    with pytest.raises(signing.SigningError, match="not found"):
        signing.sign(art, signing.SigningConfig(key=str(tmp_path / "nokey"), identity="me@x"))
    (tmp_path / "notakey").write_text("garbage")
    with pytest.raises(signing.SigningError, match="could not sign"):
        signing.sign(art, signing.SigningConfig(key=str(tmp_path / "notakey"), identity="me@x"))
