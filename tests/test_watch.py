# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The watcher end to end: fake git host API + real git repositories."""

import hashlib
import hmac
import json
import socket
import textwrap
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from gatewright.queue import Queue
from gatewright.vcs import VCSConfig, VCSEvent
from gatewright.watch import Watcher, decide

from .fakehost import FakeHost, FakePR, git

from .conftest import PY  # noqa: E402

PIPELINE = """\
version: 1
name: app
gates:
  - id: commit
  - id: test
vcs:
  provider: {kind}
  repo: acme/app
  api_url: {api}
  clone_url: {bare}
  mode: {mode}
  trusted: [alice]
  poll_interval: 10
{extra}
"""

JOBS = f"""\
jobs:
  - id: check
    gate: commit
    run: '{PY} check.py'
    cache: false
  - id: uses-secret
    gate: test
    run: '{PY} -c "import os; print(\\"secret present:\\", bool(os.environ.get(\\"DEPLOY_TOKEN\\")))"'
    secrets: [DEPLOY_TOKEN]
    cache: false
"""

CHECK = "import os, sys\nsys.exit(1 if os.path.exists('BROKEN') else 0)\n"


class Repo:
    """origin.git (bare, what the host serves), work/ (a developer pushing), home/ (where Gatewright watches)."""

    def __init__(self, tmp: Path, kind: str, mode: str = "manual", extra: str = ""):
        self.tmp, self.kind = tmp, kind
        self.bare = tmp / "origin.git"
        self.host = FakeHost(kind, self.bare).start()
        self.work = tmp / "work"
        self.work.mkdir()
        git(self.work, "init", "-q", "-b", "main")
        (self.work / ".gatewright" / "jobs").mkdir(parents=True)
        (self.work / ".gatewright" / "pipeline.yml").write_text(
            PIPELINE.format(kind=kind, api=self.host.api_url, bare=self.bare, mode=mode, extra=extra))
        (self.work / ".gatewright" / "jobs" / "jobs.yml").write_text(JOBS)
        (self.work / "check.py").write_text(CHECK)
        self.sha = self.commit("initial")
        git(tmp, "clone", "-q", "--bare", str(self.work), str(self.bare))
        git(self.work, "remote", "add", "origin", str(self.bare))
        self.home = tmp / "home"
        git(tmp, "clone", "-q", str(self.bare), str(self.home))

    def commit(self, msg: str, files: dict | None = None, branch: str | None = None) -> str:
        if branch:
            git(self.work, "checkout", "-q", "-B", branch)
        for name, content in (files or {}).items():
            p = self.work / name
            p.parent.mkdir(parents=True, exist_ok=True)
            if content is None:
                p.unlink()
            else:
                p.write_text(content)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "--allow-empty", "-m", msg)
        return git(self.work, "rev-parse", "HEAD")

    def push(self, branch: str = "main") -> None:
        git(self.work, "push", "-q", "-f", "origin", f"HEAD:refs/heads/{branch}")

    def watcher(self, logs=None) -> Watcher:
        return Watcher(str(self.home), log=(logs.append if logs is not None else None))


@pytest.fixture
def secrets(monkeypatch):
    monkeypatch.setenv("GATEWRIGHT_SECRET_VCS_TOKEN", "test-token")
    monkeypatch.setenv("GATEWRIGHT_SECRET_DEPLOY_TOKEN", "s3cret-deploy-value")


@pytest.fixture
def repo(tmp_path, secrets):
    repos = []

    def make(kind="github", mode="manual", extra=""):
        r = Repo(tmp_path, kind, mode, extra)
        repos.append(r)
        return r
    yield make
    for r in repos:
        r.host.stop()


@pytest.mark.parametrize("kind", ["github", "gitea", "gitlab"])
def test_manual_flow_reports_statuses(repo, kind):
    r = repo(kind)
    w = r.watcher()
    items = w.poll_once()
    assert [(i.kind, i.ref, i.sha, i.state) for i in items] == [("push", "main", r.sha, "pending")]
    pend = r.host.latest_status(r.sha, "gatewright/commit")
    assert pend["state"] == "pending" and f"gatewright approve {items[0].id}" in pend["description"]

    assert w.poll_once() == []                                     # nothing new
    gets = [h for m, p, h in r.host.requests if m == "GET" and "branches" in p]
    assert gets[-1].get("If-None-Match")                           # ETag sent on the second poll

    Queue(str(r.home)).approve(items[0].id, "tester")
    runs = w.run_approved()
    assert len(runs) == 1 and runs[0].commit == r.sha
    ok = {"github": "success", "gitea": "success", "gitlab": "success"}[kind]
    for gate in ("commit", "test"):
        assert r.host.latest_status(r.sha, f"gatewright/{gate}")["state"] == ok
    if kind == "gitlab":   # GitLab has its own state name for "running"
        assert any(s["state"] == "running" for s in r.host.statuses)
    item = Queue(str(r.home)).get(items[0].id)
    assert item.state == "done" and item.run_id == runs[0].run_id
    assert not (r.home / ".gatewright" / "workspaces" / f"{item.id}-{r.sha[:12]}").exists()   # cleaned up


def test_failing_commit_and_blocked_gate(repo):
    r = repo("github")
    sha = r.commit("break it", {"BROKEN": "x"})
    r.push()
    w = r.watcher()
    item = w.poll_once()[0]
    Queue(str(r.home)).approve(item.id, "t")
    run = w.run_approved()[0]
    assert run.status == "failed"
    commit = r.host.latest_status(sha, "gatewright/commit")
    test = r.host.latest_status(sha, "gatewright/test")
    assert commit["state"] == "failure" and "check" in commit["description"]
    assert test["state"] == "error" and test["description"].startswith("not run: enforced gate `commit` failed")


@pytest.mark.parametrize("mode, event, trusted, auto", [
    ("manual", VCSEvent("push", "a", "main"), True, False),
    ("auto-trusted", VCSEvent("push", "a", "main"), True, True),
    ("auto-trusted", VCSEvent("pull_request", "a", "f", "alice", 1), True, True),
    ("auto-trusted", VCSEvent("pull_request", "a", "f", "bob", 1), False, False),
    ("auto", VCSEvent("pull_request", "a", "f", "bob", 1), False, True),
    ("auto", VCSEvent("pull_request", "a", "f", "alice", 1, from_fork=True), False, False),
])
def test_trust_rules(mode, event, trusted, auto):
    d = decide(VCSConfig("github", "acme/app", mode=mode, trusted=["alice"]), event)
    assert (d.trusted, d.auto) == (trusted, auto)


def test_auto_trusted_mode_queue_states(repo):
    r = repo("github", mode="auto-trusted")
    r.commit("feature", {"f.txt": "1"}, branch="feat")
    r.push("feat")
    r.host.add_pr(FakePR(1, "feat", "alice"))
    r.host.add_pr(FakePR(2, "feat", "bob"))
    r.host.add_pr(FakePR(3, "feat", "alice", from_fork=True))
    items = {(i.kind, i.pr): i for i in r.watcher().poll_once()}
    assert items[("push", None)].state == "approved"
    assert items[("pull_request", 1)].state == "approved"
    assert items[("pull_request", 2)].state == "pending"
    assert items[("pull_request", 3)].state == "pending" and not items[("pull_request", 3)].trusted


def test_fork_pr_cannot_change_jobs_or_read_secrets(repo):
    r = repo("github", mode="auto")
    marker = r.tmp / "PWNED"
    evil_jobs = f"""\
jobs:
  - id: check
    gate: commit
    run: '{PY} -c "open(r\\"{marker}\\", \\"w\\").write(\\"x\\")"'
"""
    r.commit("evil", {".gatewright/jobs/jobs.yml": evil_jobs}, branch="evil")
    r.push("evil")
    r.host.add_pr(FakePR(7, "evil", "mallory", from_fork=True))
    w = r.watcher()
    items = w.poll_once()
    item = next(i for i in items if i.pr == 7)
    assert item.state == "pending"                         # never auto-approved, even in mode auto
    assert next(i for i in items if i.kind == "push").state == "approved"   # mode auto still runs pushes
    Queue(str(r.home)).approve(item.id, "reviewer")
    run = next(x for x in w.run_approved() if x.commit == item.sha)
    assert not marker.exists()                             # the PR's job definitions were NOT used
    secret_job = next(j for j in run.all_jobs() if j.job_id == "uses-secret")
    assert secret_job.status == "error" and "withheld" in secret_job.errors[0].human
    log = Path(run.artifact_dir, "logs", "check.log").read_text(encoding="utf-8")
    assert "PWNED" not in log
    assert r.host.comments and "Untrusted run" in r.host.comments[-1]["body"]


def test_sha_pinning_runs_the_approved_commit(repo):
    r = repo("github")
    w = r.watcher()
    first = w.poll_once()[0]
    Queue(str(r.home)).approve(first.id, "t")
    newer = r.commit("newer", {"x.txt": "2"})
    r.push()                                               # branch moved on before the run started
    run = w.run_approved()[0]
    assert run.commit == r.sha and run.commit != newer


def test_newer_commit_supersedes_pending(repo):
    r = repo("github")
    w = r.watcher()
    first = w.poll_once()[0]
    sha2 = r.commit("second", {"y.txt": "1"})
    r.push()
    second = w.poll_once()[0]
    q = Queue(str(r.home))
    assert q.get(first.id).state == "superseded" and second.sha == sha2
    assert r.host.latest_status(r.sha, "gatewright/commit")["state"] == "error"
    with pytest.raises(ValueError, match="superseded"):
        q.approve(first.id, "t")


def test_bad_token_is_explained_not_fatal(repo, monkeypatch):
    r = repo("gitea")
    monkeypatch.setenv("GATEWRIGHT_SECRET_VCS_TOKEN", "wrong")
    logs = []
    assert r.watcher(logs).poll_once() == []
    assert any("token is missing or invalid" in m for m in logs)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_webhook_end_to_end(repo, monkeypatch):
    port = _free_port()
    monkeypatch.setenv("GATEWRIGHT_SECRET_HOOK", "hook-secret")
    r = repo("github", extra=textwrap.indent(f"webhook: {{host: 127.0.0.1, port: {port}, path: /hook, secret: HOOK}}", "  "))
    w = r.watcher()
    w.start_webhook()
    try:
        body = json.dumps({"ref": "refs/heads/main", "after": r.sha, "sender": {"login": "alice"}}).encode()
        sig = "sha256=" + hmac.new(b"hook-secret", body, hashlib.sha256).hexdigest()

        def post(signature):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/hook", data=body, method="POST", headers={
                "X-GitHub-Event": "push", "X-Hub-Signature-256": signature, "Content-Type": "application/json"})
            try:
                return urllib.request.urlopen(req, timeout=10).status
            except urllib.error.HTTPError as e:
                return e.code
        assert post("sha256=" + "0" * 64) == 401
        assert Queue(str(r.home)).list() == []
        assert post(sig) == 202
        items = Queue(str(r.home)).list()
        assert len(items) == 1 and items[0].sha == r.sha
        assert urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5).status == 200
    finally:
        w._server.shutdown()


def test_cli_watch_queue_approve(repo, capsys):
    from gatewright.cli import main
    r = repo("github")
    assert main(["-C", str(r.home), "watch", "--once"]) == 0
    assert main(["-C", str(r.home), "queue"]) == 0
    out = capsys.readouterr().out
    assert "pending" in out and "branch main" in out
    item = Queue(str(r.home)).list()[0]
    assert main(["-C", str(r.home), "approve", str(item.id)]) == 0
    assert main(["-C", str(r.home), "watch", "--once"]) == 0
    assert Queue(str(r.home)).get(item.id).state == "done"
    assert main(["-C", str(r.home), "reject", str(item.id)]) == 2      # only pending items
