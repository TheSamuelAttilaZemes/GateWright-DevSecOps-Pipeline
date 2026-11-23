# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Notifications (v0.6): tell people when something needs attention.

Channels (configured under `notify:` in pipeline.yml, see docs/notifications.md):
  ntfy     push notifications to phone/desktop (https://ntfy.sh or self-hosted)
  webhook  JSON POST to any URL; Slack, Mattermost and Discord incoming webhooks
           understand it (`text` / `content` fields); optional HMAC signature header
  email    SMTP with STARTTLS (port 587) or TLS (port 465)
  matrix   message to a Matrix room via the client-server API

Events: run_passed, run_warn, run_failed, gate_failed, approval_needed,
        deployed, deploy_failed, rolled_back.

A failing notification never fails a run or a deployment: it becomes a warning.
Only the Python standard library is used (urllib, smtplib).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import smtplib
import ssl
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

from .models import RunResult
from .secrets import SecretStore

DEFAULT_EVENTS = ["run_failed", "approval_needed", "deploy_failed", "rolled_back"]
TIMEOUT = 10
RETRY_WAITS = [1.0, 3.0]     # seconds before the second and third attempt


def is_temporary(e: Exception) -> bool:
    """Worth another attempt? Server errors and network problems are; a wrong token isn't."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code >= 500 or e.code == 429
    if isinstance(e, smtplib.SMTPResponseException):
        return 400 <= e.smtp_code < 500          # SMTP: 4xx means "try again later"
    if isinstance(e, ssl.SSLCertVerificationError) or \
            isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
        return False
    return isinstance(e, (urllib.error.URLError, smtplib.SMTPServerDisconnected, ConnectionError, TimeoutError))


class Notifier:
    """Sends events to the configured channels. Collects failures in `self.warnings`."""

    def __init__(self, notify_cfg: dict | None, secrets: SecretStore, log=None):
        self.cfg = notify_cfg or {}
        self.secrets = secrets
        self.log = log or (lambda m: None)
        self.warnings: list[str] = []
        self._txn = ""
        self.sent: list[tuple[str, str]] = []   # (channel type, event), for tests and the CLI

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("channels"))

    # ------------------------------------------------------------------ events
    def run_finished(self, run: RunResult, context: str = "") -> None:
        """run_<status> for the run, plus gate_failed for every failed gate."""
        where = context or (f"{run.branch} @ {run.commit[:8]}" if run.commit else run.root)
        top = [e for j in run.all_jobs() for e in j.active_errors][:3]
        lines = [f"Pipeline {run.pipeline}: {run.status.upper()} ({where})", ""]
        lines += [f"{'✓' if g.ok else '✗'} {g.gate} ({g.mode}): {g.status}" for g in run.gates]
        if top:
            lines += ["", "Top issues:"] + [f"- [{e.severity}] {e.location or e.job_id}: {e.human[:160]}" for e in top]
        lines += ["", f"Report: {run.artifact_dir}/report.html"]
        prio = "high" if run.status == "failed" else "default"
        self.send(f"run_{run.status}", f"Gatewright: {run.pipeline} {run.status.upper()}", "\n".join(lines),
                  {"run_id": run.run_id, "status": run.status, "commit": run.commit}, prio)
        for g in run.gates:
            if g.status == "failed":
                self.send("gate_failed", f"Gatewright: gate {g.gate} failed ({run.pipeline})",
                          f"Gate {g.gate} ({g.mode}) failed in run {run.run_id} ({where}).",
                          {"run_id": run.run_id, "gate": g.gate}, "high" if g.mode == "enforced" else "default")

    def approval_needed(self, item, reason: str) -> None:
        self.send("approval_needed", f"Gatewright: approval needed for {item.label}",
                  f"{item.label}: {item.title or ''}\n{reason}\nApprove with: gatewright approve {item.id}\n{item.url}",
                  {"queue_id": item.id, "sha": item.sha}, "default")

    def deployment(self, event: str, env: str, run_id: str, detail: str) -> None:
        titles = {"deployed": f"Deployed {run_id} to {env}", "deploy_failed": f"Deploy to {env} FAILED",
                  "rolled_back": f"{env} was rolled back"}
        self.send(event, f"Gatewright: {titles.get(event, event)}", detail, {"environment": env, "run_id": run_id},
                  "high" if event != "deployed" else "default")

    # ------------------------------------------------------------------ dispatch
    def _wants(self, ch: dict, event: str) -> bool:
        return event in (ch.get("on") or self.cfg.get("on") or DEFAULT_EVENTS)

    def send(self, event: str, title: str, text: str, data: dict | None = None, priority: str = "default") -> None:
        for ch in self.cfg.get("channels", []):
            if not self._wants(ch, event):
                continue
            kind = ch["type"]
            # one id for all attempts, so Matrix drops a message that did arrive the first time
            self._txn = f"gw{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}"
            try:
                self._attempts(lambda: getattr(self, f"_send_{kind}")(ch, event, title, text, data or {}, priority))
                self.sent.append((kind, event))
            except Exception as e:  # never let a notification break the pipeline
                msg = f"notification via {kind} failed: {e}"
                self.warnings.append(msg)
                self.log(f"! {msg}")

    @staticmethod
    def _attempts(send) -> None:
        for wait in RETRY_WAITS:
            try:
                return send()
            except Exception as e:
                if not is_temporary(e):
                    raise
            time.sleep(wait)
        return send()

    def _secret(self, ch: dict, key: str) -> str | None:
        name = ch.get(key)
        if not name:
            return None
        v = self.secrets.get(name)
        if v is None:
            raise ValueError(f"secret {name} (notify.{key}) is not set")
        return v

    @staticmethod
    def _post(url: str, body: bytes, headers: dict, method: str = "POST") -> None:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # nosec B310 - configured URL
            if r.status >= 300:
                raise ValueError(f"HTTP {r.status}")

    def _send_ntfy(self, ch, event, title, text, data, priority):
        # headers are ASCII: ntfy reads other characters as an RFC 2047 encoded word
        if not title.isascii():
            title = "=?UTF-8?B?" + base64.b64encode(title.encode()).decode() + "?="
        headers = {"Title": title, "Priority": priority,
                   "Tags": "warning" if priority == "high" else "white_check_mark"}
        token = self._secret(ch, "token_secret")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._post(ch["url"], text.encode("utf-8"), headers)

    def _send_webhook(self, ch, event, title, text, data, priority):
        payload = {"source": "gatewright", "event": event, "title": title, "text": f"*{title}*\n{text}",
                   "content": f"**{title}**\n{text}"[:1900], **data}
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "gatewright"}
        secret = self._secret(ch, "secret")
        if secret:
            headers["X-Gatewright-Signature"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        self._post(ch["url"], body, headers)

    def _send_email(self, ch, event, title, text, data, priority):
        host, _, port_s = ch["smtp"].partition(":")
        port = int(port_s or 587)
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = title, ch["from"], ", ".join(ch["to"])
        msg.set_content(text)
        user, pw = self._secret(ch, "username_secret"), self._secret(ch, "password_secret")
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=ctx) as s:
                if user:
                    s.login(user, pw or "")
                s.send_message(msg)
            return
        with smtplib.SMTP(host, port, timeout=TIMEOUT) as s:
            if ch.get("starttls", True):
                s.starttls(context=ctx)
            if user:
                s.login(user, pw or "")
            s.send_message(msg)

    def _send_matrix(self, ch, event, title, text, data, priority):
        token = self._secret(ch, "token_secret")
        if not token:
            raise ValueError("matrix needs token_secret")
        room = urllib.parse.quote(ch["room"], safe="")
        url = f"{ch['homeserver'].rstrip('/')}/_matrix/client/v3/rooms/{room}/send/m.room.message/{self._txn}"
        body = json.dumps({"msgtype": "m.text", "body": f"{title}\n{text}"}).encode()
        self._post(url, body, {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}, method="PUT")
