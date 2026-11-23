# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Git host adapters and notification channels: the branches a real host rarely shows."""

import hashlib
import hmac
import json
import smtplib
import urllib.request

import pytest

from gatewright.notify import Notifier
from gatewright.secrets import SecretStore
from gatewright.vcs import VCSConfig, VCSError, VCSEvent, make_provider
from gatewright.vcs.base import Http, cached_get

SHA = "a" * 40
SHA2 = "b" * 40


class FakeHttp:
    """Records requests and answers GETs from a dict (url -> list)."""

    def __init__(self, pages=None):
        self.pages, self.calls = pages or {}, []

    def request(self, method, url, body=None, etag=None, params=None):
        self.calls.append((method, url, body))
        return 200, self.pages.get(url, []), {}


def prov(kind, token="tok", **kw):
    api = "https://api.github.com" if kind == "github" else "https://git.example/api/v1" if kind != "gitlab" \
        else "https://gitlab.example/api/v4"
    p = make_provider(VCSConfig(kind, "acme/app", api_url=api, **kw), token)
    p.http = FakeHttp()
    return p


def test_unknown_provider():
    with pytest.raises(VCSError, match="Unknown git host provider"):
        make_provider(VCSConfig("bitbucket", "a/b", api_url="x"), None)


@pytest.mark.parametrize("kind", ["gitea", "gitlab"])
def test_anonymous_git_auth(kind):
    assert prov(kind, token=None).git_auth_header() is None


def test_gitea_lists_prs_and_reports(monkeypatch):
    p = prov("gitea")
    api = "https://git.example/api/v1/repos/acme/app"
    p.http.pages = {f"{api}/branches": [{"name": "main", "commit": {"id": SHA}}, {"name": "x", "commit": {"id": SHA}}],
                    f"{api}/pulls": [{"number": 3, "title": "t", "user": {"login": "eve"},
                                      "head": {"sha": SHA2, "ref": "f", "repo": None},
                                      "base": {"repo": {"full_name": "acme/app"}}}]}
    ev = p.list_events({})
    assert [(e.kind, e.ref) for e in ev] == [("push", "main"), ("pull_request", "f")]
    assert ev[1].from_fork                      # a deleted fork (repo: null) is treated as a fork: untrusted
    p.set_status(SHA, "gatewright/test", "success", "ok", "https://ci/r1")
    p.comment(3, "hi")
    assert p.http.calls[-2][2]["target_url"] == "https://ci/r1" and p.http.calls[-1][1].endswith("/issues/3/comments")


def test_gitea_webhook_pull_request_and_other_events():
    p = prov("gitea")
    body = json.dumps({"action": "opened", "pull_request": {
        "number": 4, "head": {"sha": SHA2, "ref": "f", "repo": {"full_name": "acme/app"}},
        "base": {"repo": {"full_name": "acme/app"}}}}).encode()
    sig = hmac.new(b"s", body, hashlib.sha256).hexdigest()
    ev = p.parse_webhook({"X-Gitea-Event": "pull_request", "X-Gitea-Signature": sig}, body, "s")
    assert ev[0].pr == 4 and not ev[0].from_fork
    assert p.parse_webhook({"X-Gitea-Event": "issues", "X-Gitea-Signature": sig}, body, "s") == []


def test_gitlab_lists_mrs_and_reports():
    p = prov("gitlab")
    proj = p.project
    p.http.pages = {f"{proj}/repository/branches": [{"name": "main", "commit": {"id": SHA}}],
                    f"{proj}/merge_requests": [{"sha": SHA2, "iid": 9, "source_branch": "f", "author": {"username": "u"},
                                                "source_project_id": 2, "target_project_id": 1}]}
    ev = p.list_events({})
    assert ev[1].pr == 9 and ev[1].from_fork
    p.set_status(SHA, "gatewright/test", "failure", "bad", "https://ci/r")
    p.comment(9, "x")
    assert p.http.calls[-1][1].endswith("/merge_requests/9/notes")


def test_gitlab_webhook_ignored_actions():
    p = prov("gitlab")
    h = {"X-Gitlab-Token": "s", "X-Gitlab-Event": "Merge Request Hook"}
    closed = json.dumps({"object_attributes": {"action": "close", "iid": 1}}).encode()
    assert p.parse_webhook(h, closed, "s") == []
    assert p.parse_webhook({**h, "X-Gitlab-Event": "Pipeline Hook"}, b"{}", "s") == []


def test_github_branch_delete_and_status_url():
    p = prov("github")
    body = json.dumps({"ref": "refs/heads/main", "deleted": True, "after": "0" * 40}).encode()
    sig = "sha256=" + hmac.new(b"s", body, hashlib.sha256).hexdigest()
    assert p.parse_webhook({"X-GitHub-Event": "push", "X-Hub-Signature-256": sig}, body, "s") == []
    p.set_status(SHA, "c", "pending", "d", "https://ci")
    assert p.http.calls[-1][2]["target_url"] == "https://ci"


def test_base_defaults_and_cached_get_without_etag():
    from gatewright.vcs.base import VCSProvider

    class Minimal(VCSProvider):
        name = "minimal"
        auth_headers = lambda self: {}            # noqa: E731
        list_events = set_status = comment = parse_webhook = lambda *a, **k: None   # noqa: E731
        default_clone_url = lambda self: "https://x/a.git"   # noqa: E731
    m = Minimal(VCSConfig("github", "a/b"), None)
    assert m.fetch_refspec(VCSEvent("push", SHA, "main")) == "refs/heads/main" and m.git_auth_header() is None
    state = {}
    assert cached_get(FakeHttp({"u": [1]}), state, "u") == [1] and state == {}


def test_http_timeout_is_explained(monkeypatch):
    def slow(*a, **k):
        raise TimeoutError("timed out")
    monkeypatch.setattr(urllib.request, "urlopen", slow)
    with pytest.raises(VCSError, match="did not answer"):
        Http({}, "github").request("GET", "https://api.github.com/x")


def test_watcher_ignores_events_with_invalid_commit_ids(tmp_path, monkeypatch):
    """Regression (security): a sha like '--upload-pack=...' went into `git fetch` as an option."""
    from gatewright.watch import Watcher
    (tmp_path / ".gatewright").mkdir()
    (tmp_path / ".gatewright" / "pipeline.yml").write_text(
        "version: 1\nname: a\ngates: [{id: commit}]\nvcs: {provider: github, repo: a/b}\n")
    logs = []
    w = Watcher(str(tmp_path), provider=prov("github"), log=logs.append)
    added = w.handle_events([VCSEvent("push", "--upload-pack=touch /tmp/pwned", "main"),
                             VCSEvent("pull_request", "", "f", pr=1),
                             VCSEvent("push", SHA, "main")])
    assert [i.sha for i in added] == [SHA]
    assert sum("invalid commit id" in m for m in logs) == 2


# ---------------------------------------------------------------- notifications
def test_enabled_and_approval_message(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(Notifier, "_post", staticmethod(lambda url, body, headers, method="POST":
                                                         sent.append((url, body, headers))))
    n = Notifier({"channels": [{"type": "ntfy", "url": "https://ntfy/x", "token_secret": "NTFY"}]},
                 SecretStore(str(tmp_path)))
    assert n.enabled and not Notifier({}, SecretStore(str(tmp_path))).enabled
    monkeypatch.setenv("GATEWRIGHT_SECRET_NTFY", "tok")
    item = type("I", (), {"label": "push main @ aaaa", "title": "fix", "id": 3, "url": "u", "sha": SHA})()
    n.approval_needed(item, "manual mode")
    assert "gatewright approve 3" in sent[0][1].decode() and sent[0][2]["Authorization"] == "Bearer tok"


def test_http_error_status_is_a_warning(tmp_path, monkeypatch):
    class Resp:
        status = 500

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: Resp())
    n = Notifier({"channels": [{"type": "webhook", "url": "https://hook"}], "on": ["run_failed"]},
                 SecretStore(str(tmp_path)))
    n.send("run_failed", "t", "x")
    assert n.warnings and "HTTP 500" in n.warnings[0]


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.log = host, port, []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.log.append("starttls")

    def login(self, u, p):
        self.log.append(f"login {u}")

    def send_message(self, msg):
        self.log.append(f"send {msg['To']}")


@pytest.mark.parametrize("smtp, starttls, expect", [
    ("mail.example:465", True, ["login bot", "send a@x, b@x"]),
    ("mail.example", True, ["starttls", "login bot", "send a@x, b@x"]),
    ("mail.example:25", False, ["login bot", "send a@x, b@x"])])
def test_email_variants(tmp_path, monkeypatch, smtp, starttls, expect):
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("GATEWRIGHT_SECRET_MAIL_USER", "bot")
    monkeypatch.setenv("GATEWRIGHT_SECRET_MAIL_PW", "pw")
    n = Notifier({"channels": [{"type": "email", "smtp": smtp, "from": "ci@x", "to": ["a@x", "b@x"],
                                "username_secret": "MAIL_USER", "password_secret": "MAIL_PW", "starttls": starttls,
                                "on": ["run_failed"]}]}, SecretStore(str(tmp_path)))
    n.send("run_failed", "t", "body")
    assert not n.warnings and FakeSMTP.instances[0].log == expect


def test_matrix_needs_a_token_and_uses_unique_txn_ids(tmp_path, monkeypatch):
    urls = []
    monkeypatch.setattr(Notifier, "_post", staticmethod(lambda url, body, headers, method="POST": urls.append(url)))
    ch = {"type": "matrix", "homeserver": "https://m.example/", "room": "!r:m", "on": ["run_failed", "gate_failed"]}
    n = Notifier({"channels": [ch]}, SecretStore(str(tmp_path)))
    n.send("run_failed", "t", "x")
    assert "matrix needs token_secret" in n.warnings[0]
    monkeypatch.setenv("GATEWRIGHT_SECRET_MX", "tok")
    n = Notifier({"channels": [{**ch, "token_secret": "MX"}]}, SecretStore(str(tmp_path)))
    n.send("run_failed", "t", "x")
    n.send("gate_failed", "t", "y")
    assert len(urls) == 2 and urls[0] != urls[1]   # regression: same-millisecond messages were dropped
