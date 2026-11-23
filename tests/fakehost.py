# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""A fake git host for tests: speaks the documented REST API of GitHub, Gitea/Forgejo
or GitLab, backed by a REAL bare git repository (branches, PR refs, checkouts are
real git). Records every status, comment and request so tests can assert on them."""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def git(cwd, *args, check=True) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                            "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(cwd), "PATH": __import__("os").environ["PATH"]})
    if check and p.returncode != 0:
        raise RuntimeError(f"git {args}: {p.stderr}")
    return p.stdout.strip()


@dataclass
class FakePR:
    number: int
    branch: str            # ref in the bare repo holding the PR head (e.g. a fork's branch)
    author: str
    from_fork: bool = False
    title: str = "A change"


@dataclass
class FakeHost:
    kind: str              # github | gitea | gitlab
    bare: Path
    repo: str = "acme/app"
    token: str = "test-token"
    prs: list[FakePR] = field(default_factory=list)
    statuses: list[dict] = field(default_factory=list)
    comments: list[dict] = field(default_factory=list)
    requests: list[tuple] = field(default_factory=list)
    fail_with: int | None = None   # make every API call fail with this HTTP code

    def start(self) -> "FakeHost":
        host = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj=None, headers=None):
                body = b"" if obj is None else json.dumps(obj).encode()
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self):
                h = self.headers
                if host.kind == "github":
                    return h.get("Authorization") == f"Bearer {host.token}"
                if host.kind == "gitea":
                    return h.get("Authorization") == f"token {host.token}"
                return h.get("PRIVATE-TOKEN") == host.token

            def _route(self, method):
                url = urllib.parse.urlparse(self.path)
                host.requests.append((method, url.path, dict(self.headers)))
                if host.fail_with:
                    return self._send(host.fail_with, {"message": "nope"})
                if not self._authorized():
                    return self._send(401, {"message": "Bad credentials"})
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}") if length else {}
                result = host.handle(method, url.path, data)
                if result is None:
                    return self._send(404, {"message": "Not Found"})
                code, obj = result
                if method == "GET":
                    etag = '"' + hashlib.sha1(json.dumps(obj, sort_keys=True).encode()).hexdigest() + '"'
                    if self.headers.get("If-None-Match") == etag:
                        return self._send(304, None, {"ETag": etag})
                    return self._send(code, obj, {"ETag": etag})
                return self._send(code, obj)

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()

    @property
    def api_url(self) -> str:
        port = self.server.server_address[1]
        return {"github": f"http://127.0.0.1:{port}", "gitea": f"http://127.0.0.1:{port}/api/v1",
                "gitlab": f"http://127.0.0.1:{port}/api/v4"}[self.kind]

    # --- real git data -----------------------------------------------------------
    def branches(self) -> dict[str, str]:
        out = git(self.bare, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads")
        return dict(line.split() for line in out.splitlines() if line)

    def add_pr(self, pr: FakePR) -> None:
        """Expose `pr.branch` as the PR head ref the way the host does."""
        sha = git(self.bare, "rev-parse", f"refs/heads/{pr.branch}")
        ref = f"refs/merge-requests/{pr.number}/head" if self.kind == "gitlab" else f"refs/pull/{pr.number}/head"
        git(self.bare, "update-ref", ref, sha)
        self.prs.append(pr)

    # --- API ---------------------------------------------------------------------
    def handle(self, method: str, path: str, data: dict):
        if self.kind == "gitlab":
            prefix = f"/api/v4/projects/{urllib.parse.quote(self.repo, safe='')}"
            if not path.startswith(prefix):
                return None
            rest = path[len(prefix):]
            if method == "GET" and rest == "/repository/branches":
                return 200, [{"name": n, "commit": {"id": s}} for n, s in self.branches().items()]
            if method == "GET" and rest.startswith("/repository/branches/"):
                name = urllib.parse.unquote(rest.split("/", 3)[3])
                sha = self.branches().get(name)
                return (200, {"name": name, "commit": {"id": sha}}) if sha else None
            if method == "GET" and rest == "/merge_requests":
                return 200, [{"iid": p.number, "title": p.title, "web_url": f"http://x/mr/{p.number}",
                              "sha": git(self.bare, "rev-parse", f"refs/heads/{p.branch}"), "source_branch": p.branch,
                              "source_project_id": 99 if p.from_fork else 1, "target_project_id": 1,
                              "author": {"username": p.author}} for p in self.prs]
            if method == "POST" and rest.startswith("/statuses/"):
                self.statuses.append({"sha": rest.split("/")[-1], "context": data.get("name"), **data})
                return 201, {}
            if method == "POST" and rest.endswith("/notes"):
                self.comments.append({"pr": int(rest.split("/")[2]), "body": data["body"]})
                return 201, {}
            return None
        prefix = ("/api/v1" if self.kind == "gitea" else "") + f"/repos/{self.repo}"
        if not path.startswith(prefix):
            return None
        rest = path[len(prefix):]
        sha_key = "id" if self.kind == "gitea" else "sha"
        if method == "GET" and rest == "/branches":
            return 200, [{"name": n, "commit": {sha_key: s}} for n, s in self.branches().items()]
        if method == "GET" and rest.startswith("/branches/"):
            name = urllib.parse.unquote(rest.split("/", 2)[2])
            sha = self.branches().get(name)
            return (200, {"name": name, "commit": {sha_key: sha}}) if sha else None
        if method == "GET" and rest == "/pulls":
            return 200, [{"number": p.number, "title": p.title, "html_url": f"http://x/pull/{p.number}",
                          "head": {"sha": git(self.bare, "rev-parse", f"refs/heads/{p.branch}"), "ref": p.branch,
                                   "repo": {"full_name": "evil/fork" if p.from_fork else self.repo}},
                          "base": {"repo": {"full_name": self.repo}}, "user": {"login": p.author}} for p in self.prs]
        if method == "POST" and rest.startswith("/statuses/"):
            self.statuses.append({"sha": rest.split("/")[-1], **data})
            return 201, {}
        if method == "POST" and rest.endswith("/comments"):
            self.comments.append({"pr": int(rest.split("/")[2]), "body": data["body"]})
            return 201, {}
        return None

    def latest_status(self, sha: str, context: str) -> dict | None:
        for s in reversed(self.statuses):
            if s["sha"] == sha and s.get("context") == context:
                return s
        return None
