# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Git host adapters: webhook verification/parsing, URLs, and error messages."""

import hashlib
import hmac
import json

import pytest

from gatewright.vcs import VCSConfig, VCSError, make_provider
from gatewright.vcs.base import Http

SECRET = "s3cr3t"


def gh_sig(body):
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def gitea_sig(body):
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def prov(kind, **kw):
    return make_provider(VCSConfig(kind, "acme/app", api_url=kw.pop("api_url", "https://git.example/api/v1"), **kw), "tok")


PR_GH = {"action": "synchronize", "pull_request": {
    "number": 5, "title": "Fix", "html_url": "u", "user": {"login": "bob"},
    "head": {"sha": "abc", "ref": "fix", "repo": {"full_name": "bob/app"}}, "base": {"repo": {"full_name": "acme/app"}}}}


@pytest.mark.parametrize("kind, event_header, sig_header, sign", [
    ("github", "X-GitHub-Event", "X-Hub-Signature-256", gh_sig),
    ("gitea", "X-Gitea-Event", "X-Gitea-Signature", gitea_sig),
    ("forgejo", "X-Forgejo-Event", "X-Forgejo-Signature", gitea_sig),
])
def test_push_webhook(kind, event_header, sig_header, sign):
    p = prov(kind, api_url="https://api.github.com" if kind == "github" else "https://git.example/api/v1")
    body = json.dumps({"ref": "refs/heads/main", "after": "f00d", "sender": {"login": "al"},
                       "pusher": {"login": "al"}}).encode()
    ev = p.parse_webhook({event_header: "push", sig_header: sign(body)}, body, SECRET)
    assert [(e.kind, e.sha, e.ref) for e in ev] == [("push", "f00d", "main")]
    with pytest.raises(VCSError, match="signature"):
        p.parse_webhook({event_header: "push", sig_header: sign(body + b" ")}, body, SECRET)
    with pytest.raises(VCSError):
        p.parse_webhook({event_header: "push"}, body, SECRET)      # unsigned
    other = json.dumps({"ref": "refs/heads/experiment", "after": "f00d"}).encode()
    assert p.parse_webhook({event_header: "push", sig_header: sign(other)}, other, SECRET) == []   # unwatched


def test_github_pull_request_webhook_marks_forks():
    p = prov("github", api_url="")
    body = json.dumps(PR_GH).encode()
    [ev] = p.parse_webhook({"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": gh_sig(body)}, body, SECRET)
    assert (ev.kind, ev.pr, ev.sha, ev.author, ev.from_fork) == ("pull_request", 5, "abc", "bob", True)
    closed = json.dumps({**PR_GH, "action": "closed"}).encode()
    assert p.parse_webhook({"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": gh_sig(closed)}, closed, SECRET) == []


def test_gitlab_webhooks_use_token():
    p = prov("gitlab", api_url="https://gitlab.example/api/v4")
    push = json.dumps({"ref": "refs/heads/main", "after": "beef", "user_username": "al"}).encode()
    [ev] = p.parse_webhook({"X-Gitlab-Event": "Push Hook", "X-Gitlab-Token": SECRET}, push, SECRET)
    assert (ev.kind, ev.sha, ev.author) == ("push", "beef", "al")
    mr = json.dumps({"object_attributes": {"iid": 3, "action": "update", "source_branch": "x", "title": "t",
                                           "last_commit": {"id": "c0ffee"}, "source_project_id": 9,
                                           "target_project_id": 1, "url": "u"},
                     "user": {"username": "eve"}}).encode()
    [ev] = p.parse_webhook({"X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": SECRET}, mr, SECRET)
    assert (ev.pr, ev.sha, ev.from_fork) == (3, "c0ffee", True)
    with pytest.raises(VCSError, match="token"):
        p.parse_webhook({"X-Gitlab-Event": "Push Hook", "X-Gitlab-Token": "nope"}, push, SECRET)
    deleted = json.dumps({"ref": "refs/heads/main", "after": "0" * 40}).encode()
    assert p.parse_webhook({"X-Gitlab-Event": "Push Hook", "X-Gitlab-Token": SECRET}, deleted, SECRET) == []


def test_clone_urls_and_refspecs():
    from gatewright.vcs import VCSEvent
    gh = make_provider(VCSConfig("github", "acme/app"), "t")
    assert gh.clone_url == "https://github.com/acme/app.git"
    ghe = make_provider(VCSConfig("github", "acme/app", api_url="https://ghe.corp/api/v3"), "t")
    assert ghe.clone_url == "https://ghe.corp/acme/app.git"
    assert prov("gitea").clone_url == "https://git.example/acme/app.git"
    gl = make_provider(VCSConfig("gitlab", "grp/sub/app"), "t")
    assert gl.clone_url == "https://gitlab.com/grp/sub/app.git"
    assert gl.project.endswith("/projects/grp%2Fsub%2Fapp")
    pr = VCSEvent("pull_request", "a", "b", pr=4)
    assert gh.fetch_refspec(pr) == "refs/pull/4/head" and gl.fetch_refspec(pr) == "refs/merge-requests/4/head"
    assert gh.git_auth_header().startswith("Basic ")
    assert make_provider(VCSConfig("github", "acme/app"), None).git_auth_header() is None


def test_gitea_requires_api_url():
    p = make_provider(VCSConfig("gitea", "acme/app"), "t")
    with pytest.raises(VCSError, match="api_url"):
        p.list_events({})


@pytest.mark.parametrize("code, words", [(401, "token is missing or invalid"), (403, "rate limit"),
                                          (404, "repository was not found"), (422, "rejected")])
def test_http_errors_are_explained(code, words):
    assert words in Http({}, "GitHub")._explain(code, "GET", "https://x/y", b"{}")


def test_unreachable_host_is_explained():
    h = Http({}, "Gitea/Forgejo", timeout=2)
    with pytest.raises(VCSError, match="Could not reach"):
        h.request("GET", "http://127.0.0.1:9/api/v1/repos/a/b/branches")
