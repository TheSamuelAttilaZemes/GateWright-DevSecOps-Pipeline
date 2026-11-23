# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Gitea and Forgejo adapter (also Codeberg). Both expose the same API under /api/v1.

`vcs.api_url` is required, e.g. https://codeberg.org/api/v1 or https://git.example.com/api/v1.
Token scopes: *repository: read and write* (statuses and comments)."""

from __future__ import annotations

import base64
import json

from .base import (ERROR, FAILURE, PENDING, RUNNING, SUCCESS, VCSError, VCSEvent, VCSProvider, cached_get,
                   verify_hmac_sha256, watched_branches)

_STATE = {PENDING: "pending", RUNNING: "pending", SUCCESS: "success", FAILURE: "failure", ERROR: "error"}

_BRANCH = {"name": 0, "commit": {"id": 0}}
_PULL = {"number": 0, "title": 0, "html_url": 0, "user": {"login": 0}, "head": {"sha": 0, "ref": 0, "repo": {"full_name": 0}},
         "base": {"repo": {"full_name": 0}}}


class GiteaProvider(VCSProvider):
    name = "Gitea/Forgejo"

    @property
    def api(self) -> str:
        if not self.cfg.api_url:
            raise VCSError("Gitea/Forgejo needs `vcs.api_url`, e.g. https://codeberg.org/api/v1")
        return self.cfg.api_url.rstrip("/")

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"token {self.token}"} if self.token else {}

    def default_clone_url(self) -> str:
        return self.api.split("/api/")[0] + f"/{self.cfg.repo}.git"

    def git_auth_header(self) -> str | None:
        if not self.token:
            return None
        return "Basic " + base64.b64encode(f"gatewright:{self.token}".encode()).decode()

    def fetch_refspec(self, event: VCSEvent) -> str:
        return f"refs/pull/{event.pr}/head" if event.kind == "pull_request" else f"refs/heads/{event.ref}"

    def list_events(self, state: dict) -> list[VCSEvent]:
        events = []
        for b in watched_branches(self.http, state, self.cfg, f"{self.api}/repos/{self.cfg.repo}/branches",
                                  {"limit": 50}, _BRANCH):
            events.append(VCSEvent("push", b["commit"]["id"], b["name"]))
        if self.cfg.pull_requests:
            for p in cached_get(self.http, state, f"{self.api}/repos/{self.cfg.repo}/pulls",
                                {"state": "open", "limit": 50}, _PULL):
                events.append(self._pr_event(p))
        return events

    def _pr_event(self, p: dict) -> VCSEvent:
        head_repo = ((p.get("head") or {}).get("repo") or {}).get("full_name")
        base_repo = ((p.get("base") or {}).get("repo") or {}).get("full_name") or self.cfg.repo
        return VCSEvent("pull_request", p["head"]["sha"], p["head"].get("ref", ""),
                        author=(p.get("user") or {}).get("login", ""), pr=int(p["number"]),
                        from_fork=head_repo != base_repo, title=p.get("title", ""), url=p.get("html_url", ""))

    def set_status(self, sha, context, state, description, target_url=""):
        body = {"state": _STATE[state], "context": context, "description": description[:140]}
        if target_url:
            body["target_url"] = target_url
        self.http.request("POST", f"{self.api}/repos/{self.cfg.repo}/statuses/{sha}", body)

    def comment(self, pr, body):
        self.http.request("POST", f"{self.api}/repos/{self.cfg.repo}/issues/{pr}/comments", {"body": body})

    def parse_webhook(self, headers, body, secret):
        h = {k.lower(): v for k, v in headers.items()}
        sig = h.get("x-forgejo-signature") or h.get("x-gitea-signature", "")
        if not verify_hmac_sha256(secret, body, sig):
            raise VCSError("Webhook signature is missing or wrong (X-Gitea-Signature / X-Forgejo-Signature). "
                           "Check the webhook secret.")
        kind = h.get("x-forgejo-event") or h.get("x-gitea-event", "")
        data = json.loads(body or b"{}")
        if kind == "push":
            ref = data.get("ref", "")
            branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ""
            if not branch or not self.cfg.branch_watched(branch) or set(data.get("after", "")) == {"0"}:
                return []
            pusher = data.get("pusher") or {}
            return [VCSEvent("push", data["after"], branch, author=pusher.get("login") or pusher.get("username", ""))]
        if kind == "pull_request" and self.cfg.pull_requests and \
                data.get("action") in ("opened", "synchronized", "reopened"):
            return [self._pr_event(data["pull_request"])]
        return []
