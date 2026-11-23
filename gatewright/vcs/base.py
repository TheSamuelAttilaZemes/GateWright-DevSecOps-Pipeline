# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Shared pieces for git host adapters: config, events, the provider interface
and a small JSON-over-HTTP client (urllib only, ETag-aware, human errors)."""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

# Commit status states, as Gatewright names them. Each adapter maps them to its host's names.
PENDING, RUNNING, SUCCESS, FAILURE, ERROR = "pending", "running", "success", "failure", "error"


class VCSError(Exception):
    """A git host call failed. The message is written for humans and says what to do."""

    def __init__(self, message: str, code: int = 0, retry_at: float = 0.0):
        super().__init__(message)
        self.code = code               # HTTP status, if the host answered
        self.retry_at = retry_at       # rate limited: ask again after this time (seconds since 1970)


@dataclass
class VCSConfig:
    """The `vcs:` section of pipeline.yml (see docs/git-integration.md)."""
    provider: str
    repo: str                                   # owner/name (GitLab: group/sub/name)
    api_url: str = ""
    clone_url: str = ""
    token_secret: str = "VCS_TOKEN"
    branches: list[str] = field(default_factory=lambda: ["main", "master"])
    pull_requests: bool = True
    mode: str = "manual"                        # manual | auto-trusted | auto
    trusted: list[str] = field(default_factory=list)
    gates: list[str] = field(default_factory=list)
    poll_interval: int = 60
    untrusted: str = "run"                      # approved untrusted commits: run | containers | refuse
    parallel: int = 1                           # commits run at the same time
    fetch_depth: int = 50                       # commits of history to fetch (0: all)
    submodules: bool = False                    # trusted commits only
    lfs: bool = False                           # trusted commits only
    webhook: dict = field(default_factory=dict)
    report_url: str = ""
    comment: bool = True
    status_prefix: str = "gatewright"

    @classmethod
    def from_dict(cls, d: dict) -> "VCSConfig":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def branch_watched(self, branch: str) -> bool:
        return any(fnmatch.fnmatch(branch, pat) for pat in self.branches)


@dataclass
class VCSEvent:
    """Something to (maybe) run: a new commit on a watched branch, or a pull request head."""
    kind: str                       # push | pull_request
    sha: str
    ref: str                        # branch name, or the PR's source branch
    author: str = ""
    pr: int | None = None           # pull/merge request number
    from_fork: bool = False         # PR from another repository: never trusted automatically
    title: str = ""
    url: str = ""

    @property
    def key(self) -> str:
        """Stable identity of the *thing* (branch or PR), not of the commit."""
        return f"pr:{self.pr}" if self.kind == "pull_request" else f"branch:{self.ref}"


MAX_PAGES = 100
MISSING_FOR = 600     # seconds until a branch that doesn't exist is looked for again


def rate_limit_end(code: int, headers: dict, now: float) -> float:
    """When a rate limited host may be asked again (0: this is not a rate limit)."""
    low = {k.lower(): v for k, v in headers.items()}
    if code not in (403, 429):
        return 0.0
    wait = str(low.get("retry-after", "")).strip()
    if wait.isdigit():
        return now + int(wait)
    reset = str(low.get("x-ratelimit-reset") or low.get("ratelimit-reset") or "").strip()
    left = str(low.get("x-ratelimit-remaining", low.get("ratelimit-remaining", ""))).strip()
    if left == "0" and reset.isdigit():
        return float(reset)
    return now + 60 if code == 429 else 0.0


def slim(item, shape):
    """Only the fields of `item` that `shape` names: the poll state is kept in the database, and
    thousands of pull request texts are not needed there."""
    if not isinstance(shape, dict) or not isinstance(item, dict):
        return item
    return {k: slim(item.get(k), v) for k, v in shape.items() if k in item}


def watched_branches(http: "Http", state: dict, cfg: "VCSConfig", url: str, params: dict, shape: dict) -> list:
    """The watched branches. Plain names are asked for one by one, so a repository with thousands of
    branches costs one request per name; patterns (`release/*`) need the whole list."""
    if not any(c in name for name in cfg.branches for c in "*?["):
        found = []
        for name in cfg.branches:
            one = f"{url}/{urllib.parse.quote(name, safe='')}"
            if time.time() - state.get(f"missing:{one}", 0) < MISSING_FOR:
                continue      # e.g. `master` of the default [main, master]: don't spend a request on it every time
            try:
                found += cached_get(http, state, one, shape=shape)
                state.pop(f"missing:{one}", None)
            except VCSError as e:
                if e.code != 404:
                    raise
                state[f"missing:{one}"] = time.time()
        if found:
            return found
        # none of them exists, or the repository can't be seen: the list tells which
    return [b for b in cached_get(http, state, url, params, shape) if cfg.branch_watched(b["name"])]


def cached_get(http: "Http", state: dict, url: str, params: dict | None = None, shape: dict | None = None) -> list:
    """GET every page of a list endpoint with ETag caching: unchanged data costs no rate limit
    on GitHub and little everywhere. `state` keeps the last ETag and body per page.

    There is a next page if the host says so (Link header: some hosts send fewer items than
    asked for) or the page is full (an unchanged page doesn't tell that a new one follows it).
    Only the page number is ours to change: URLs from the header are never followed, so the
    token can't be sent elsewhere.
    """
    params = params or {}
    size = int(params.get("per_page") or params.get("limit") or 0)
    out: list = []
    for page in range(1, MAX_PAGES + 1):
        query = {**params, "page": page} if page > 1 else params
        key = url + ("?" + urllib.parse.urlencode(query) if query else "")
        status, data, headers = http.request("GET", url, etag=state.get(f"etag:{key}"), params=query)
        low = {k.lower(): v for k, v in headers.items()}
        if status == 304:
            data, more = state.get(f"body:{key}", []), state.get(f"more:{key}", False)
        else:
            data = [data] if isinstance(data, dict) else data or []
            data, more = [slim(x, shape) for x in data], 'rel="next"' in low.get("link", "")
            if low.get("etag"):
                state[f"etag:{key}"], state[f"body:{key}"], state[f"more:{key}"] = low["etag"], data, more
        out += data
        if not more and not (size and len(data) >= size):
            return out
    raise VCSError(f"{url} has more than {MAX_PAGES} pages. Watch fewer branches, or use webhooks.")


class VCSProvider(ABC):
    """Interface every git host adapter implements."""

    name = "base"

    def __init__(self, cfg: VCSConfig, token: str | None):
        self.cfg = cfg
        self.token = token
        self.http = Http(self.auth_headers(), self.name)

    # --- what adapters must provide -----------------------------------------
    @abstractmethod
    def auth_headers(self) -> dict[str, str]:
        """HTTP headers that authenticate API calls."""

    @abstractmethod
    def list_events(self, state: dict) -> list[VCSEvent]:
        """Current heads of watched branches and open PRs. `state` is a dict the
        adapter may use to keep ETags between polls (it is persisted for it)."""

    @abstractmethod
    def set_status(self, sha: str, context: str, state: str, description: str, target_url: str = "") -> None:
        """Set a commit status (state is one of PENDING/RUNNING/SUCCESS/FAILURE/ERROR)."""

    @abstractmethod
    def comment(self, pr: int, body: str) -> None:
        """Comment on a pull/merge request."""

    @abstractmethod
    def parse_webhook(self, headers: dict[str, str], body: bytes, secret: str) -> list[VCSEvent]:
        """Verify a webhook delivery (raise VCSError if the signature is wrong) and return its events."""

    @abstractmethod
    def default_clone_url(self) -> str:
        """HTTPS clone URL of the repository."""

    def fetch_refspec(self, event: VCSEvent) -> str:
        """Ref to fetch so that `event.sha` is available (the branch, or the PR head ref)."""
        return f"refs/heads/{event.ref}"

    def git_auth_header(self) -> str | None:
        """Authorization header value for `git fetch` over HTTPS, or None."""
        return None

    # --- shared helpers --------------------------------------------------------
    def git_auth_env(self) -> dict[str, str]:
        """Environment that gives git the token, for this host only (a submodule or LFS server
        elsewhere must not get it). Not on the command line, where `ps` would show it."""
        auth, url = self.git_auth_header(), urllib.parse.urlsplit(self.clone_url)
        if not auth or url.scheme not in ("http", "https"):
            return {}
        return {"GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"http.{url.scheme}://{url.netloc.rpartition('@')[2]}/.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: {auth}"}

    @property
    def clone_url(self) -> str:
        return self.cfg.clone_url or self.default_clone_url()


def verify_hmac_sha256(secret: str, body: bytes, signature: str, prefix: str = "") -> bool:
    """Constant-time check of a hex HMAC-SHA256 signature (optionally prefixed, e.g. 'sha256=')."""
    if not secret or not signature:
        return False
    expected = prefix + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


class Http:
    """Minimal JSON client. Turns HTTP errors into VCSError with a fix in plain words."""

    def __init__(self, headers: dict[str, str], host_name: str, timeout: int = 20):
        self.headers = {"Accept": "application/json", "User-Agent": "gatewright", **headers}
        self.host_name = host_name
        self.timeout = timeout

    def request(self, method: str, url: str, body=None, etag: str | None = None,
                params: dict | None = None) -> tuple[int, object, dict]:
        """Return (status, parsed JSON or None, response headers). 304 returns (304, None, headers)."""
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        data = None
        headers = dict(self.headers)
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if etag:
            headers["If-None-Match"] = etag
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # nosec B310 - configured API URL
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw.strip() else None), dict(resp.headers)
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, None, dict(e.headers)
            until = rate_limit_end(e.code, dict(e.headers or {}), time.time())
            if until:
                raise VCSError(f"{self.host_name} limits how often it can be asked (HTTP {e.code}). Asking again at "
                               f"{time.strftime('%H:%M:%S', time.localtime(until))}. Poll less often "
                               "(`vcs.poll_interval`), watch fewer branches, or use webhooks.", e.code, until) from None
            raise VCSError(self._explain(e.code, method, url, e.read()[:300]), e.code) from None
        except urllib.error.URLError as e:
            raise VCSError(f"Could not reach the {self.host_name} API at {url}: {e.reason}. "
                           "Check `vcs.api_url`, your network and proxy settings.") from None
        except (TimeoutError, OSError) as e:
            raise VCSError(f"The {self.host_name} API at {url} did not answer ({e}).") from None

    def _explain(self, code: int, method: str, url: str, body: bytes) -> str:
        detail = body.decode("utf-8", "replace").strip()
        hint = {
            401: "The API token is missing or invalid. Set it (see `vcs.token_secret`) and check it hasn't expired.",
            403: "The token is not allowed to do this, or you hit the API rate limit. Give the token permission "
                 "to read the repository and write commit statuses, or poll less often.",
            404: "The repository was not found, or the token cannot see it. Check `vcs.repo` and the token's access.",
            422: "The host rejected the request data (often: the commit doesn't exist on the host yet).",
        }.get(code, "")
        return f"{self.host_name} API {method} {url} failed with HTTP {code}. {hint} {detail}".strip()

