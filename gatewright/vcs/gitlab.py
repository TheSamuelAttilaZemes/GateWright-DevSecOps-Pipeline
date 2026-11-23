# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""GitLab adapter (gitlab.com and self-managed).

`vcs.repo` is the project path (group/subgroup/name). Token: a project or personal
access token with the `api` scope (statuses and comments need write access)."""

from __future__ import annotations

import base64
import hmac
import json
import urllib.parse

from .base import ERROR, FAILURE, PENDING, RUNNING, SUCCESS, VCSError, VCSEvent, VCSProvider, cached_get, watched_branches

_STATE = {PENDING: "pending", RUNNING: "running", SUCCESS: "success", FAILURE: "failed", ERROR: "failed"}

_BRANCH = {"name": 0, "commit": {"id": 0}}
_MERGE = {"iid": 0, "sha": 0, "title": 0, "web_url": 0, "source_branch": 0, "source_project_id": 0, "target_project_id": 0,
          "author": {"username": 0}}


class GitLabProvider(VCSProvider):
    name = "GitLab"

    @property
    def api(self) -> str:
        return (self.cfg.api_url or "https://gitlab.com/api/v4").rstrip("/")

    @property
    def project(self) -> str:
        return f"{self.api}/projects/{urllib.parse.quote(self.cfg.repo, safe='')}"

    def auth_headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self.token} if self.token else {}

    def default_clone_url(self) -> str:
        return self.api.split("/api/")[0] + f"/{self.cfg.repo}.git"

    def git_auth_header(self) -> str | None:
        if not self.token:
            return None
        return "Basic " + base64.b64encode(f"oauth2:{self.token}".encode()).decode()

    def fetch_refspec(self, event: VCSEvent) -> str:
        return f"refs/merge-requests/{event.pr}/head" if event.kind == "pull_request" else f"refs/heads/{event.ref}"

    def list_events(self, state: dict) -> list[VCSEvent]:
        events = []
        for b in watched_branches(self.http, state, self.cfg, f"{self.project}/repository/branches",
                                  {"per_page": 100}, _BRANCH):
            events.append(VCSEvent("push", b["commit"]["id"], b["name"]))
        if self.cfg.pull_requests:
            for m in cached_get(self.http, state, f"{self.project}/merge_requests",
                                {"state": "opened", "per_page": 100}, _MERGE):
                events.append(VCSEvent(
                    "pull_request", m["sha"], m.get("source_branch", ""),
                    author=(m.get("author") or {}).get("username", ""), pr=int(m["iid"]),
                    from_fork=m.get("source_project_id") != m.get("target_project_id"),
                    title=m.get("title", ""), url=m.get("web_url", "")))
        return events

    def set_status(self, sha, context, state, description, target_url=""):
        body = {"state": _STATE[state], "name": context, "description": description[:255]}
        if target_url:
            body["target_url"] = target_url
        try:
            self.http.request("POST", f"{self.project}/statuses/{sha}", body)
        except VCSError as e:
            # GitLab refuses to set the state a status already has (a commit can be queued
            # twice: as a branch and as a merge request)
            if "Cannot transition status via" not in str(e) or f"from :{_STATE[state]}" not in str(e):
                raise

    def comment(self, pr, body):
        self.http.request("POST", f"{self.project}/merge_requests/{pr}/notes", {"body": body})

    def parse_webhook(self, headers, body, secret):
        h = {k.lower(): v for k, v in headers.items()}
        if not secret or not hmac.compare_digest(h.get("x-gitlab-token", ""), secret):
            raise VCSError("Webhook token is missing or wrong (X-Gitlab-Token). Check the webhook secret token.")
        kind = h.get("x-gitlab-event", "")
        data = json.loads(body or b"{}")
        if kind == "Push Hook":
            ref = data.get("ref", "")
            branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ""
            if not branch or not self.cfg.branch_watched(branch) or set(data.get("after", "")) == {"0"}:
                return []
            return [VCSEvent("push", data["after"], branch, author=data.get("user_username", ""))]
        if kind == "Merge Request Hook" and self.cfg.pull_requests:
            a = data.get("object_attributes") or {}
            if a.get("action") not in ("open", "update", "reopen"):
                return []
            return [VCSEvent("pull_request", (a.get("last_commit") or {}).get("id", ""), a.get("source_branch", ""),
                             author=(data.get("user") or {}).get("username", ""), pr=int(a["iid"]),
                             from_fork=a.get("source_project_id") != a.get("target_project_id"),
                             title=a.get("title", ""), url=a.get("url", ""))]
        return []
