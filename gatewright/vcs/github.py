# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""GitHub adapter (github.com and GitHub Enterprise Server).

API reference: REST v3. Token: a fine-grained personal access token with
*Contents: read*, *Commit statuses: read and write* and *Pull requests: read and write*
(write only needed for PR comments)."""

from __future__ import annotations

import base64
import json

from .base import (ERROR, FAILURE, PENDING, RUNNING, SUCCESS, VCSError, VCSEvent, VCSProvider, cached_get,
                   verify_hmac_sha256, watched_branches)

_STATE = {PENDING: "pending", RUNNING: "pending", SUCCESS: "success", FAILURE: "failure", ERROR: "error"}

_BRANCH = {"name": 0, "commit": {"sha": 0}}
_PULL = {"number": 0, "title": 0, "html_url": 0, "user": {"login": 0}, "head": {"sha": 0, "ref": 0, "repo": {"full_name": 0}},
         "base": {"repo": {"full_name": 0}}}


class GitHubProvider(VCSProvider):
    name = "GitHub"

    @property
    def api(self) -> str:
        return (self.cfg.api_url or "https://api.github.com").rstrip("/")

    def auth_headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def default_clone_url(self) -> str:
        if self.cfg.api_url and "api.github.com" not in self.cfg.api_url:  # GitHub Enterprise: https://host/api/v3
            return self.cfg.api_url.split("/api/")[0].rstrip("/") + f"/{self.cfg.repo}.git"
        return f"https://github.com/{self.cfg.repo}.git"

    def git_auth_header(self) -> str | None:
        if not self.token:
            return None
        return "Basic " + base64.b64encode(f"x-access-token:{self.token}".encode()).decode()

    def fetch_refspec(self, event: VCSEvent) -> str:
        return f"refs/pull/{event.pr}/head" if event.kind == "pull_request" else f"refs/heads/{event.ref}"

    def list_events(self, state: dict) -> list[VCSEvent]:
        events = []
        for b in watched_branches(self.http, state, self.cfg, f"{self.api}/repos/{self.cfg.repo}/branches",
                                  {"per_page": 100}, _BRANCH):
            events.append(VCSEvent("push", b["commit"]["sha"], b["name"]))
        if self.cfg.pull_requests:
            for p in cached_get(self.http, state, f"{self.api}/repos/{self.cfg.repo}/pulls",
                                {"state": "open", "per_page": 100}, _PULL):
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
        if not verify_hmac_sha256(secret, body, h.get("x-hub-signature-256", ""), "sha256="):
            raise VCSError("Webhook signature is missing or wrong (X-Hub-Signature-256). Check the webhook secret.")
        kind = h.get("x-github-event", "")
        data = json.loads(body or b"{}")
        if kind == "push":
            ref = data.get("ref", "")
            if data.get("deleted") or not ref.startswith("refs/heads/"):
                return []
            branch = ref[len("refs/heads/"):]
            if not self.cfg.branch_watched(branch):
                return []
            return [VCSEvent("push", data["after"], branch, author=(data.get("sender") or {}).get("login", ""))]
        if kind == "pull_request" and self.cfg.pull_requests and \
                data.get("action") in ("opened", "synchronize", "reopened"):
            return [self._pr_event(data["pull_request"])]
        return []
