# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Resource-aware scheduler.

A job starts only when:
  * all its dependencies have finished OK (otherwise it is BLOCKED),
  * fewer than `max_parallel` jobs are running,
  * the declared cpu/mem of running jobs + this job fits the budget,
  * live CPU and RAM usage are below the profile limits.
If nothing is running, the next job always starts (no deadlock on big jobs).

A watchdog kills the newest job when RAM crosses `kill_mem_percent`, so a
runaway test cannot freeze the whole machine.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from .graph import toposort
from .hardware import Budget, live_usage
from .models import BLOCKED, ERROR, SKIPPED, Job, JobResult, fmt_bytes
from .runner import CANCEL_REASON, JobHandle

EventFn = Callable[[str, dict], None]


class Scheduler:
    """Runs one gate's jobs in dependency order within the resource budget."""
    def __init__(self, budget: Budget, on_event: EventFn | None = None,
                 stop: threading.Event | None = None, poll: float = 0.1):
        self.budget = budget
        self.on_event = on_event or (lambda k, d: None)
        self.stop = stop or threading.Event()
        self.poll = poll
        self.peak_parallel = 0

    def _fits(self, job: Job, running: dict[str, tuple[Job, JobHandle, threading.Thread]]) -> tuple[bool, str]:
        if not running:
            return True, ""
        b = self.budget
        if len(running) >= b.max_parallel:
            return False, f"max_parallel={b.max_parallel} reached"
        cpu = sum(j.cpu for j, _, _ in running.values()) + job.cpu
        mem = sum(j.mem for j, _, _ in running.values()) + job.mem
        if cpu > b.cpu:
            return False, f"cpu budget ({cpu:g}/{b.cpu:g})"
        if mem > b.mem:
            return False, f"memory budget ({fmt_bytes(mem)}/{fmt_bytes(b.mem)})"
        u = live_usage()
        if u["mem_percent"] >= b.max_mem_percent:
            return False, f"system RAM at {u['mem_percent']:.0f}%"
        if u["cpu_percent"] >= b.max_cpu_percent:
            return False, f"system CPU at {u['cpu_percent']:.0f}%"
        return True, ""

    def run(self, jobs: list[Job], execute: Callable[[Job, JobHandle], JobResult],
            external: dict[str, JobResult] | None = None) -> dict[str, JobResult]:
        """`external` holds results of dependencies from earlier gates."""
        external = external or {}
        ids = {j.id for j in jobs}
        pending = toposort(jobs)
        results: dict[str, JobResult] = {}
        running: dict[str, tuple[Job, JobHandle, threading.Thread]] = {}
        waiting_reason: dict[str, str] = {}
        lock = threading.Lock()
        last_sample = 0.0

        def worker(job: Job, handle: JobHandle) -> None:
            try:
                res = execute(job, handle)
            except Exception as e:  # never let one job crash the scheduler
                res = JobResult(job.id, job.gate, job.category, ERROR, note=f"internal error: {e}")
                from .models import JobError
                res.errors = [JobError(job.id, "crash", "high", f"Gatewright hit an internal error while running "
                                       f"this job: {e}", "This is a Gatewright bug; please report it with the log.",
                                       raw=repr(e))]
            with lock:
                results[job.id] = res

        def dep_state(job: Job) -> str:
            """ok | wait | bad"""
            for d in job.needs:
                r = results.get(d) or external.get(d)
                if r is None:
                    if d in ids:
                        return "wait"
                    continue  # dependency not part of this run
                if not r.ok:
                    return "bad"
            return "ok"

        while pending or running:
            # reap finished
            for jid in [k for k, (_, _, t) in running.items() if not t.is_alive()]:
                running.pop(jid)
                with lock:
                    self.on_event("job_end", {"result": results[jid]})

            if self.stop.is_set():
                for _, h, _ in running.values():
                    h.kill(CANCEL_REASON)
                for job in pending:
                    results[job.id] = JobResult(job.id, job.gate, job.category, SKIPPED, note="cancelled")
                    self.on_event("job_end", {"result": results[job.id]})
                pending = []
                time.sleep(self.poll)
                continue

            # watchdog
            now = time.monotonic()
            if now - last_sample > 1.0:
                last_sample = now
                u = live_usage()
                self.on_event("resources", {"usage": u, "running": list(running)})
                if running and u["mem_percent"] >= self.budget.kill_mem_percent:
                    newest = max(running.values(), key=lambda x: x[1].started_at)
                    self.on_event("message", {"level": "error", "text":
                                  f"RAM at {u['mem_percent']:.0f}% - stopping `{newest[0].id}` to protect the system"})
                    newest[1].kill(f"system memory reached {u['mem_percent']:.0f}% "
                                   f"(limit {self.budget.kill_mem_percent:.0f}%), job was using "
                                   f"{fmt_bytes(newest[1].rss)}")

            # start what we can
            for job in list(pending):
                st = dep_state(job)
                if st == "wait":
                    continue
                if st == "bad":
                    failed = [d for d in job.needs if (results.get(d) or external.get(d)) and
                              not (results.get(d) or external.get(d)).ok]
                    res = JobResult(job.id, job.gate, job.category, BLOCKED,
                                    note=f"dependency failed: {', '.join(failed)}")
                    results[job.id] = res
                    pending.remove(job)
                    self.on_event("job_end", {"result": res})
                    continue
                ok, why = self._fits(job, running)
                if not ok:
                    if waiting_reason.get(job.id) != why:
                        waiting_reason[job.id] = why
                        self.on_event("job_waiting", {"job_id": job.id, "reason": why})
                    continue
                handle = JobHandle(job)
                t = threading.Thread(target=worker, args=(job, handle), daemon=True, name=f"job-{job.id}")
                running[job.id] = (job, handle, t)
                pending.remove(job)
                self.peak_parallel = max(self.peak_parallel, len(running))
                self.on_event("job_start", {"job_id": job.id})
                t.start()
            time.sleep(self.poll)
        return results
