# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The watcher (v0.5): turn git host events into approved, SHA-pinned pipeline runs.

    git host ──poll (ETag) / webhook (signed)──▶ events ──▶ Queue (pending | auto-approved)
                                                                   │  gatewright approve N
                                                                   ▼
                     commit statuses ◀── Engine.run(checkout of exactly that SHA) ◀── claim_next()

Trust rules (see docs/git-integration.md#trust-and-approval):
  * a push to a watched branch is trusted: only people with write access can push
  * a pull request is trusted when it is NOT from a fork and its author is in `vcs.trusted`
  * pull requests from forks are NEVER approved automatically, run WITHOUT secrets,
    and use the job definitions from your local (trusted) .gatewright/, not the PR's
`vcs.mode`: manual (approve everything by hand) | auto-trusted (auto-approve trusted
events) | auto (auto-approve everything except forks).
"""

from __future__ import annotations

import os
import queue
import re
import shutil
import stat
import subprocess  # nosec B404 - git commands with fixed argv
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from .config import load_pipeline
from .engine import Engine, Selection
from .hardware import detect, make_budget, pick_profile
from .models import BLOCKED, CANCELLED, FAILED, PASSED, SKIPPED, WARN, GateResult, RunResult
from .queue import DONE, ERROR as Q_ERROR, Queue, QueueItem
from .secrets import SecretStore
from .vcs import VCSConfig, VCSError, VCSEvent, VCSProvider, make_provider
from .vcs.base import ERROR, FAILURE, PENDING, RUNNING, SUCCESS
from .winsupport import long_path

LogFn = Callable[[str], None]


def _clear_readonly_and_retry(func, path, _exc) -> None:
    """rmtree error handler: git writes its object files read-only, and Windows refuses to
    delete read-only files. Make the file writable and try once more."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_tree(path: Path | str, ignore_errors: bool = False) -> None:
    """shutil.rmtree that also removes git checkouts on Windows (read-only objects)."""
    handler = {"onexc": _clear_readonly_and_retry} if sys.version_info >= (3, 12) \
        else {"onerror": _clear_readonly_and_retry}
    try:
        shutil.rmtree(long_path(path), **handler)
    except OSError:
        if not ignore_errors:
            raise


@dataclass
class Decision:
    trusted: bool
    auto: bool
    reason: str


def decide(cfg: VCSConfig, ev: VCSEvent) -> Decision:
    """Is this event trusted, and may it run without a human approving it?"""
    if ev.kind == "push":
        trusted, why = True, "push to a watched branch (needs write access)"
    elif ev.from_fork:
        return Decision(False, False, "pull request from a fork: needs manual approval, runs without secrets")
    elif ev.author and ev.author in cfg.trusted:
        trusted, why = True, f"pull request by trusted author {ev.author}"
    else:
        trusted, why = False, f"pull request by {ev.author or 'unknown author'} (not in vcs.trusted)"
    auto = cfg.mode == "auto" or (cfg.mode == "auto-trusted" and trusted)
    return Decision(trusted, auto, why)


SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")   # SHA-1 or SHA-256 commit id


def gate_state(g: GateResult) -> tuple[str, str]:
    """Commit status + description for a finished gate."""
    issues = sum(len(j.active_errors) for j in g.jobs)
    if g.status == CANCELLED:
        return ERROR, "cancelled before it finished (not tested)"
    if g.status in (PASSED, SKIPPED):
        return SUCCESS, "passed" if g.status == PASSED else "skipped"
    if g.status == WARN:
        return SUCCESS, f"passed with warnings ({issues} issue(s))"
    if g.status == BLOCKED:
        return ERROR, f"not run: {g.note or 'blocked'}"[:140]
    if g.mode == "advisory":
        return SUCCESS, f"advisory gate failed ({issues} issue(s)), not blocking"
    failed = [j.job_id for j in g.jobs if j.status in (FAILED, "error")]
    return FAILURE, f"failed: {', '.join(failed[:4])} ({issues} issue(s))"[:140]


class Watcher:
    """Polls/receives events for one project and runs approved commits."""

    def __init__(self, home: str, provider: VCSProvider | None = None, log: LogFn | None = None,
                 notifier=None):
        self.home = str(Path(home).resolve())        # your trusted local project (state lives here)
        self.pipeline = load_pipeline(self.home)
        if not self.pipeline.vcs:
            raise ValueError("pipeline.yml has no `vcs:` section. See docs/git-integration.md.")
        self.cfg = VCSConfig.from_dict(self.pipeline.vcs)
        self.secrets = SecretStore(self.home)
        if provider is None:
            provider = make_provider(self.cfg, self.secrets.get(self.cfg.token_secret))
        self.provider = provider
        self.queue = Queue(self.home)
        self.log = log or (lambda m: None)
        self.notifier = notifier
        self.stop = threading.Event()
        self.paused_until = 0.0
        self._later: queue.Queue = queue.Queue()    # calls to the git host that a webhook doesn't wait for
        threading.Thread(target=self._work_later, daemon=True, name="statuses").start()
        self._server: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------------ contexts
    def gates_for_runs(self) -> list[str]:
        return self.cfg.gates or [g.id for g in self.pipeline.gates if g.enabled]

    def context(self, gate: str) -> str:
        return f"{self.cfg.status_prefix}/{gate}"

    def _status_all(self, sha: str, state: str, description: str, target_url: str = "") -> None:
        for g in self.gates_for_runs():
            self._safe(self.provider.set_status, sha, self.context(g), state, description, target_url)

    def _safe(self, fn, *args):
        """Git host hiccups must never crash the watcher; they are logged instead."""
        try:
            fn(*args)
            return True
        except VCSError as e:
            self.log(f"! {e}")
            return False

    # ------------------------------------------------------------------ intake
    def _work_later(self) -> None:
        while True:
            fn, args = self._later.get()
            try:
                fn(*args)
            except Exception as e:  # one failed status must not end all later ones
                self.log(f"! {e}")
            finally:
                self._later.task_done()

    def handle_events(self, events: list[VCSEvent], later: bool = False) -> list[QueueItem]:
        """Queue new events (each commit once), mark superseded ones, set 'waiting' statuses.
        `later`: talk to the git host in the background (a webhook has to be answered at once)."""
        def do(fn, *args):
            if later:
                self._later.put((fn, args))
            else:
                fn(*args)
        added = []
        for ev in events:
            if not SHA_RE.fullmatch(ev.sha or ""):
                # never pass anything but a real commit id to git (e.g. "--upload-pack=..." would be an option)
                self.log(f"! ignored an event for {ev.ref or '?'} with an invalid commit id {ev.sha[:50]!r}")
                continue
            d = decide(self.cfg, ev)
            item, superseded = self.queue.add(ev, d.trusted, d.auto)
            for old in superseded:
                do(self._status_all, old.sha, ERROR, f"superseded by {ev.sha[:8]}")
                self.log(f"  #{old.id} superseded by a newer commit on the same {old.label.split(' @')[0]}")
            if item is None:
                continue
            added.append(item)
            if d.auto:
                do(self._status_all, ev.sha, PENDING, "queued (auto-approved)")
                self.log(f"+ #{item.id} {item.label}: auto-approved ({d.reason})")
            else:
                do(self._status_all, ev.sha, PENDING, f"waiting for approval: gatewright approve {item.id}")
                self.log(f"+ #{item.id} {item.label}: waiting for approval ({d.reason})")
                if self.notifier:
                    do(self.notifier.approval_needed, item, d.reason)
        return added

    def poll_once(self) -> list[QueueItem]:
        state = self.queue.get_state("poll_state", {})
        before = self.queue.last_id()     # items a webhook adds while we poll are newer than the list
        try:
            events = self.provider.list_events(state)
        except VCSError as e:
            self.log(f"! {e}")
            self.paused_until = e.retry_at
            # what was read so far is not read again when a big repository hits its limit half-way
            self.queue.set_state("poll_state", state)
            return []
        self.queue.set_state("poll_state", state)
        if self.cfg.pull_requests:
            for old in self.queue.close_missing({e.pr for e in events if e.pr}, before):
                self._status_all(old.sha, ERROR, "the pull request was closed before it was approved")
                self.log(f"  #{old.id} {old.label}: closed or merged, removed from the queue")
        return self.handle_events(events)

    # ------------------------------------------------------------------ running
    def checkout(self, item: QueueItem) -> Path:
        """Fetch exactly item.sha into a fresh workspace and verify it (SHA pinning)."""
        ws = Path(self.home) / ".gatewright" / "workspaces" / f"{item.id}-{item.sha[:12]}"
        if ws.exists():
            remove_tree(ws)
        ws.mkdir(parents=True)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **self.provider.git_auth_env()}

        def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
            # longpaths: deep workspaces on Windows; child git processes (submodules) inherit it
            p = subprocess.run(["git", "-c", "core.longpaths=true", *args], cwd=ws, env=env, capture_output=True, text=True,  # nosec B603 B607
                               timeout=600, check=False)
            if check and p.returncode != 0:
                raise VCSError(f"git {args[0]} failed: {p.stderr.strip()[-400:]}")
            return p

        git("init", "-q")
        url = self.provider.clone_url
        refspec = self.provider.fetch_refspec(item.event)
        n = self.cfg.fetch_depth
        fetched = git("fetch", "-q", "--no-tags", *(["--depth", str(n)] if n else []), url, refspec, check=False)
        if fetched.returncode != 0 or git("cat-file", "-e", f"{item.sha}^{{commit}}", check=False).returncode != 0:
            # the branch moved on (or the ref is gone): ask for the commit itself
            git("fetch", "-q", "--no-tags", *(["--depth", "1"] if n else []), url, item.sha)
        git("-c", "advice.detachedHead=false", "checkout", "-q", "--detach", item.sha)
        head = git("rev-parse", "HEAD").stdout.strip()
        if head != item.sha:
            raise VCSError(f"Checked out {head[:12]} but the approved commit is {item.sha[:12]}; refusing to run.")
        # an untrusted commit must not choose which other servers we contact
        if item.trusted and (self.cfg.submodules or self.cfg.lfs):
            git("remote", "add", "origin", url)
            if self.cfg.submodules:
                git("submodule", "update", "-q", "--init", "--recursive")
            if self.cfg.lfs:
                git("lfs", "install", "--local")
                git("lfs", "pull", "origin")
        return ws

    def _untrusted_problem(self, pipe) -> str:
        """Why an approved, untrusted commit may not run (empty: it may)."""
        if self.cfg.untrusted == "refuse":
            return "untrusted commits are never run (vcs.untrusted: refuse)"
        if self.cfg.untrusted == "containers":
            gates = self.gates_for_runs()
            open_jobs = [j.id for j in pipe.jobs if j.gate in gates and (not j.image or j.network)]
            if open_jobs:
                return ("untrusted commits only run in containers without network (vcs.untrusted: containers), "
                        f"but these jobs don't: {', '.join(open_jobs)}. Set `image:` and `network: false`.")
        return ""

    def run_item(self, item: QueueItem) -> RunResult | None:
        """Run one claimed item and report statuses. Never raises."""
        self._later.join()     # a late "waiting" must not replace this run's statuses
        self.log(f"▶ #{item.id} {item.label}")
        try:
            ws = self.checkout(item)
        except (VCSError, OSError, subprocess.SubprocessError) as e:
            self.queue.finish(item.id, Q_ERROR, note=str(e)[:500])
            self._status_all(item.sha, ERROR, "could not fetch the commit")
            self.log(f"! #{item.id} {e}")
            return None
        try:
            if item.trusted:
                pipe = load_pipeline(ws) if (ws / ".gatewright" / "pipeline.yml").exists() else load_pipeline(self.home)
            else:
                # untrusted code must not choose what runs: use YOUR job definitions on THEIR code
                pipe = load_pipeline(self.home)
                problem = self._untrusted_problem(pipe)
                if problem:
                    raise VCSError(problem)
            pipe.root = str(ws)
            if self.cfg.parallel > 1:   # share the machine between the runs
                most = make_budget(hw := detect(pipe.root), pick_profile(hw, pipe.profile), pipe.limits).max_parallel
                pipe.limits = {**pipe.limits, "max_parallel": max(1, most // self.cfg.parallel)}
            self._status_all(item.sha, RUNNING, f"running (#{item.id})")

            def on_event(kind, data):
                if kind == "gate_end":
                    g = data["gate"]
                    st, desc = gate_state(g)
                    self._safe(self.provider.set_status, item.sha, self.context(g.gate), st, desc,
                               self._report_url(run_holder.get("id", "")))
                elif kind == "run_start":
                    run_holder["id"] = data["run"].run_id

            run_holder: dict = {}
            engine = Engine(pipe, on_event, data_root=self.home, trusted=item.trusted)
            run = engine.run(Selection(gates=[g for g in self.gates_for_runs() if pipe.gate(g)] or None))
            ran = {g.gate for g in run.gates}
            for g in self.gates_for_runs():
                if g not in ran:
                    self._safe(self.provider.set_status, item.sha, self.context(g), SUCCESS, "no jobs selected")
            if item.pr and self.cfg.comment:
                self._safe(self.provider.comment, item.pr, pr_comment(run, item, self._report_url(run.run_id)))
            self.queue.finish(item.id, DONE, run.run_id, run.status)
            self.log(f"■ #{item.id} {run.status.upper()} (run {run.run_id})")
            return run
        except Exception as e:  # config errors in the checkout, etc. - report, keep watching
            self.queue.finish(item.id, Q_ERROR, note=str(e)[:500])
            self._status_all(item.sha, ERROR, f"gatewright error: {str(e)[:100]}")
            self.log(f"! #{item.id} {e}")
            return None
        finally:
            remove_tree(ws, ignore_errors=True)

    def _report_url(self, run_id: str) -> str:
        return self.cfg.report_url.format(run_id=run_id) if self.cfg.report_url and run_id else ""

    def run_approved(self, limit: int | None = None) -> list[RunResult]:
        """Run every approved item (`vcs.parallel` at a time), at most `limit` of them."""
        runs: list[RunResult] = []
        lock, taken = threading.Lock(), []

        def drain():
            while True:
                with lock:
                    if self.stop.is_set() or (limit is not None and len(taken) >= limit):
                        return
                    item = self.queue.claim_next()
                    if not item:
                        return
                    taken.append(item)
                r = self.run_item(item)
                if r:
                    with lock:
                        runs.append(r)
        others = [threading.Thread(target=drain, name=f"run-{i}") for i in range(1, self.cfg.parallel)]
        for t in others:
            t.start()
        drain()
        for t in others:
            t.join()
        return runs

    # ------------------------------------------------------------------ webhook
    def start_webhook(self) -> tuple[str, int] | None:
        wh = self.cfg.webhook or {}
        if not wh:
            return None
        secret = self.secrets.get(wh.get("secret", "VCS_WEBHOOK_SECRET")) or ""
        if not secret:
            raise ValueError(f"Webhook configured but its secret `{wh.get('secret', 'VCS_WEBHOOK_SECRET')}` is not set.")
        watcher, path = self, wh.get("path", "/hook")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep the console clean
                pass

            def _reply(self, code: int, text: str):
                body = text.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))   # or a proxy waits for the connection to close
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._reply(200 if self.path == "/health" else 404, "ok" if self.path == "/health" else "not found")

            def do_POST(self):
                if self.path != path:
                    return self._reply(404, "not found")
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if length < 0:
                    return self._reply(400, "bad Content-Length")
                if length > 5_000_000:
                    return self._reply(413, "payload too large")
                body = self.rfile.read(length)
                try:
                    events = watcher.provider.parse_webhook(dict(self.headers), body, secret)
                except VCSError as e:
                    watcher.log(f"! rejected webhook from {self.client_address[0]}: {e}")
                    return self._reply(401, "invalid signature")
                except (ValueError, KeyError) as e:
                    return self._reply(400, f"bad payload: {e}")
                items = watcher.handle_events(events, later=True)
                self._reply(202, f"queued {len(items)}")

        self._server = ThreadingHTTPServer((wh.get("host", "127.0.0.1"), int(wh.get("port", 8787))), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True, name="webhook").start()
        return self._server.server_address[:2]

    # ------------------------------------------------------------------ main loop
    def loop(self, once: bool = False) -> None:
        """Poll, run approved items, repeat. `once`: a single pass (cron, tests)."""
        try:
            addr = self.start_webhook()
            if addr:
                self.log(f"webhook listening on http://{addr[0]}:{addr[1]}{self.cfg.webhook.get('path', '/hook')}")
            next_poll = 0.0
            while not self.stop.is_set():
                self.queue.heartbeat()
                if time.monotonic() >= next_poll:
                    self.poll_once()
                    # a rate limited host is left alone until it says so
                    wait = max(10, int(self.cfg.poll_interval), self.paused_until - time.time())
                    next_poll = time.monotonic() + wait
                self.run_approved()
                if once:
                    break
                self.stop.wait(2)
        finally:
            if self._server:
                self._server.shutdown()


def pr_comment(run: RunResult, item: QueueItem, url: str) -> str:
    """Markdown summary posted on the pull/merge request."""
    icon = {"passed": "✅", "warn": "⚠️", "failed": "❌"}.get(run.status, "ℹ️")
    lines = [f"### {icon} Gatewright Pipeline: {run.status.upper()}",
             f"Commit `{item.sha[:12]}` · run `{run.run_id}`" + (f" · [report]({url})" if url else ""),
             "", "| Gate | Mode | Result |", "|---|---|---|"]
    for g in run.gates:
        st, desc = gate_state(g)
        lines.append(f"| `{g.gate}` | {g.mode} | {'✅' if st == SUCCESS else '❌' if st == FAILURE else '⏸️'} {desc} |")
    errs = [e for j in run.all_jobs() for e in j.active_errors][:5]
    if errs:
        lines += ["", "**Top issues**", ""]
        lines += [f"- **{e.severity}** `{e.location or e.job_id}`: {e.human[:200]}" for e in errs]
    if not item.trusted:
        lines += ["", "_Untrusted run: secrets were withheld and the job definitions came from the base "
                  "configuration, not from this pull request._"]
    return "\n".join(lines)
