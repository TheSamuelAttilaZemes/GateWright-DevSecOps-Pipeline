# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Release notes from real git history, and notifications to local test servers."""

import hashlib
import hmac
import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from gatewright.config import load_pipeline
from gatewright.engine import Engine, Selection
from gatewright.notify import Notifier
from gatewright.releasenotes import classify, generate
from gatewright.secrets import SecretStore

from .conftest import BASIC_PIPELINE
from .fakehost import git


# ---------------------------------------------------------------- release notes
def test_release_notes_group_commits(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    for msg in ["initial", "feat: first feature"]:
        git(tmp_path, "commit", "-q", "--allow-empty", "-m", msg)
    git(tmp_path, "tag", "v1.0.0")
    for msg in ["feat(api): add export endpoint", "fix: crash on empty cart", "security: escape user input",
                "docs: install guide", "chore: bump deps", "refactor!: drop python 3.9", "tidy things up"]:
        git(tmp_path, "commit", "-q", "--allow-empty", "-m", msg)
    notes = generate(str(tmp_path))                          # default range: since the last tag
    assert "v1.0.0..HEAD" in notes and "7 commit(s)" in notes
    assert "first feature" not in notes                      # before the tag
    for heading, text in [("## Features", "**api:** add export endpoint"), ("## Fixes", "crash on empty cart"),
                          ("## Security", "escape user input"), ("## Documentation", "install guide"),
                          ("## Maintenance", "bump deps"), ("## Breaking changes", "drop python 3.9"),
                          ("## Other changes", "tidy things up")]:
        assert heading in notes and text in notes
    assert notes.index("## Breaking changes") < notes.index("## Features")


def test_breaking_change_footer():
    assert classify({"subject": "feat: new auth", "body": "BREAKING CHANGE: tokens rotate"})[0] == "Breaking changes"


def test_release_notes_quality_section(project):
    root = project(BASIC_PIPELINE, 'jobs:\n  - {id: a, gate: commit, run: "echo hi"}\n', git=True)
    run = Engine(load_pipeline(root)).run(Selection(reports=["md"]))
    notes = generate(str(root), run=run)
    assert "## Quality" in notes and run.run_id in notes and "Open findings" in notes


# ---------------------------------------------------------------- notifications
class Capture:
    """HTTP server recording every request (ntfy, webhook, Matrix all speak HTTP)."""

    def __init__(self, fail=False):
        self.calls = []
        cap = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                cap.calls.append((self.command, self.path, dict(self.headers), body))
                self.send_response(500 if fail else 200)
                self.end_headers()
            do_POST = do_PUT = _any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


class SMTPStub(socketserver.ThreadingTCPServer):
    """Just enough SMTP to receive one message (no TLS)."""
    allow_reuse_address = True

    def __init__(self):
        self.messages = []
        stub = self

        class H(socketserver.StreamRequestHandler):
            def handle(self):
                w = lambda s: self.wfile.write((s + "\r\n").encode())  # noqa: E731
                w("220 stub")
                data, in_data = [], False
                for raw in self.rfile:
                    line = raw.decode().rstrip("\r\n")
                    if in_data:
                        if line == ".":
                            stub.messages.append("\n".join(data))
                            in_data, data = False, []
                            w("250 ok")
                        else:
                            data.append(line)
                        continue
                    cmd = line.split(" ")[0].upper()
                    if cmd == "EHLO":
                        w("250 stub")
                    elif cmd == "DATA":
                        in_data = True
                        w("354 go")
                    elif cmd == "QUIT":
                        w("221 bye")
                        return
                    else:
                        w("250 ok")
        super().__init__(("127.0.0.1", 0), H)
        threading.Thread(target=self.serve_forever, daemon=True).start()


@pytest.fixture
def capture():
    c = Capture()
    yield c
    c.server.shutdown()


def test_channels_receive_messages(capture, monkeypatch, tmp_path):
    monkeypatch.setenv("GATEWRIGHT_SECRET_HOOKSIG", "sig-secret")
    monkeypatch.setenv("GATEWRIGHT_SECRET_MXTOKEN", "mx-token")
    smtp = SMTPStub()
    cfg = {"on": ["run_failed", "deployed"], "channels": [
        {"type": "ntfy", "url": f"{capture.url}/alerts"},
        {"type": "webhook", "url": f"{capture.url}/hook", "secret": "HOOKSIG"},
        {"type": "matrix", "homeserver": capture.url, "room": "!room:example.org", "token_secret": "MXTOKEN"},
        {"type": "email", "smtp": f"127.0.0.1:{smtp.server_address[1]}", "from": "ci@x", "to": ["me@x"],
         "starttls": False},
    ]}
    n = Notifier(cfg, SecretStore(str(tmp_path)))
    n.deployment("deployed", "staging", "run-1", "staging now runs run-1")
    n.deployment("rolled_back", "staging", "run-1", "not subscribed")       # filtered by `on`
    assert n.warnings == [] and sorted(k for k, _ in n.sent) == ["email", "matrix", "ntfy", "webhook"]
    by_path = {path.split("/")[1] if path.count("/") else path: (m, h, b) for m, path, h, b in capture.calls}
    m, h, b = by_path["alerts"]
    assert h["Title"].startswith("Gatewright: Deployed") and b"staging now runs run-1" in b
    m, h, b = by_path["hook"]
    assert json.loads(b)["event"] == "deployed"
    assert h["X-Gatewright-Signature"] == "sha256=" + hmac.new(b"sig-secret", b, hashlib.sha256).hexdigest()
    m, h, b = by_path["_matrix"]
    assert m == "PUT" and h["Authorization"] == "Bearer mx-token" and "%21room%3Aexample.org" in [
        p for _, p, _, _ in capture.calls if p.startswith("/_matrix")][0]
    assert len(smtp.messages) == 1 and "Subject: Gatewright: Deployed run-1 to staging" in smtp.messages[0]
    smtp.shutdown()


def test_failing_channel_is_a_warning(tmp_path):
    bad = Capture(fail=True)
    n = Notifier({"channels": [{"type": "ntfy", "url": bad.url, "on": ["deployed"]}]}, SecretStore(str(tmp_path)))
    n.deployment("deployed", "x", "r", "d")
    assert n.sent == [] and "ntfy failed" in n.warnings[0]
    bad.server.shutdown()


def test_missing_secret_is_a_warning(tmp_path):
    n = Notifier({"channels": [{"type": "matrix", "homeserver": "http://127.0.0.1:9", "room": "!r",
                                "token_secret": "NOPE", "on": ["deployed"]}]}, SecretStore(str(tmp_path)))
    n.deployment("deployed", "x", "r", "d")
    assert "NOPE" in n.warnings[0]


def test_engine_notifies_failed_run(project, capture):
    pipe = BASIC_PIPELINE + f"notify:\n  channels:\n    - {{type: webhook, url: '{capture.url}/hook'}}\n"
    root = project(pipe, 'jobs:\n  - {id: bad, gate: commit, run: "exit 3"}\n')
    run = Engine(load_pipeline(root)).run(Selection(reports=["md"]))
    assert run.status == "failed"
    events = [json.loads(b)["event"] for _, _, _, b in capture.calls]
    assert events == ["run_failed"]                          # gate_failed isn't in the default events
    payload = json.loads(capture.calls[0][3])
    assert payload["run_id"] == run.run_id and "bad" in payload["text"]
