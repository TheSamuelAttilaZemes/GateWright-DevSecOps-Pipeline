# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Terminal UI built with Textual. A front-end over the same Engine the CLI uses.

Layout:  left column  - gate/category checkboxes, live CPU/RAM, status
         right column - job table (top) and tabs: Errors | Log | Report | History

The engine runs in a worker thread (`_worker`). Engine events arrive on that
thread and are forwarded to the UI thread with `call_from_thread`, then
handled in `_handle_event`, which updates the job table and the log.
Never touch widgets from the worker thread directly.

Keys:  o open a local build (tui_open.py) · r run · d dry run · f re-run failed · x cancel · c cache on/off
       m mode override · e errors tab · l log tab · p report tab · q quit

To add a key: add a `Binding` to BINDINGS and an `action_<name>` method.
"""

from __future__ import annotations

import functools
import sqlite3
from pathlib import Path

from rich.console import Group
from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.css.query import NoMatches
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (DataTable, Footer, Header, Label, Markdown, RichLog, SelectionList, Static,
                             TabbedContent, TabPane)
from textual.widgets.selection_list import Selection as SLItem

from .engine import Engine, Selection
from .hardware import detect, live_usage, make_budget, pick_profile
from .history import History
from .models import ERROR, FAILED, JobResult, Pipeline, RunResult, fmt_bytes
from .render import STYLE, error_panel, status_text
from .runner import describe_command, tool_available
from .tui_open import OpenBuild


def _bar(pct: float, width: int = 18) -> Text:
    filled = int(round(pct / 100 * width))
    style = "green" if pct < 70 else "yellow" if pct < 85 else "bold red"
    t = Text("█" * filled, style=style)
    t.append("░" * (width - filled), style="dim")
    t.append(f" {pct:5.1f}%")
    return t


def _nothing() -> Pipeline:
    """Stands in until something is opened (Gatewright started outside a project)."""
    from .local import state_dir
    here = state_dir() / "_none"
    here.mkdir(parents=True, exist_ok=True)
    return Pipeline(name="nothing open (press o)", root=str(here), gates=[], jobs=[])


def _while_mounted(fn):
    """For UI updates started by timers, table events or the run thread: if the app is
    closing, its widgets are already gone - skip the update instead of raising NoMatches."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except NoMatches:
            return None
    return wrapper


class GatewrightApp(App):
    """The Textual application. Create with a loaded Pipeline and call .run()."""
    TITLE = "Gatewright Pipeline"
    CSS = """
    Screen { layout: vertical; }
    #main { height: 1fr; }
    #side { width: 38; border-right: solid $primary-background; padding: 0 1; }
    #side Label { margin-top: 1; text-style: bold; color: $text-muted; }
    #gates, #cats { height: auto; max-height: 12; border: round $primary-background; }
    #resources { height: auto; border: round $primary-background; padding: 0 1; }
    #status { height: auto; padding: 0 1; }
    #center { width: 1fr; }
    #jobs { height: 45%; border: round $primary-background; }
    #tabs { height: 1fr; }
    #errors { padding: 0 1; }
    RichLog { background: $surface; }
    """
    BINDINGS = [
        Binding("r", "run", "Run"),
        Binding("o", "open_build", "Open build"),
        Binding("d", "dry_run", "Dry run"),
        Binding("f", "rerun_failed", "Re-run failed"),
        Binding("x", "cancel", "Cancel"),
        Binding("c", "toggle_cache", "Cache"),
        Binding("m", "toggle_mode", "Mode"),
        Binding("e", "tab('errors')", "Errors"),
        Binding("l", "tab('log')", "Log"),
        Binding("p", "tab('report')", "Report"),
        Binding("u", "tab('queue')", "Queue"),
        Binding("a", "approve", "Approve"),
        Binding("n", "reject", "Reject"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, pipeline: Pipeline | None, data_root: str | None = None, trusted: bool = True,
                 project: Pipeline | None = None, pick: bool = False):
        """`pipeline`: what to run (None: nothing yet). `project`: the project to go back to, if that is
        something else. `pick`: ask for a local build at once."""
        super().__init__()
        self.project = project or (pipeline if data_root is None else None)
        self.pick = pick
        self.results: dict[str, JobResult] = {}
        self.row_status: dict[str, str] = {}
        self.last_run: RunResult | None = None
        self.running = False
        self.use_cache = True
        self.mode_override: str | None = None
        self.selected_job: str | None = None
        self._use(pipeline or _nothing(), data_root, trusted)

    def _use(self, pipeline: Pipeline, data_root: str | None = None, trusted: bool = True) -> None:
        """Make `pipeline` the one that is shown and run."""
        self.p = pipeline
        self.data_root = data_root or pipeline.root
        self.engine = Engine(pipeline, self._event_from_thread, data_root=data_root, trusted=trusted)
        self.history = History(self.data_root)
        self.hw = detect(pipeline.root)
        self.profile = pick_profile(self.hw, pipeline.profile)
        self.budget = make_budget(self.hw, self.profile, pipeline.limits)

    # ---------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        """Build the widget tree (see the layout in the module docstring)."""
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="side"):
                yield Label("GATES (space to toggle)")
                yield SelectionList[str](
                    *[SLItem(f"{g.id}  [{g.mode}]", g.id, g.enabled) for g in self.p.gates], id="gates")
                yield Label("CATEGORIES")
                yield SelectionList[str](*[SLItem(c, c, True) for c in ("dev", "sec", "ops")], id="cats")
                yield Label("MACHINE")
                yield Static(id="resources")
                yield Static(id="status")
            with Vertical(id="center"):
                yield DataTable(id="jobs", cursor_type="row", zebra_stripes=True)
                with TabbedContent(id="tabs", initial="errors"):
                    with TabPane("Errors", id="errors"):
                        yield VerticalScroll(Static(id="errors-body"))
                    with TabPane("Log", id="log"):
                        yield RichLog(id="logview", wrap=True, markup=False, highlight=False, max_lines=5000)
                    with TabPane("Report", id="report"):
                        yield VerticalScroll(Markdown(id="report-md"))
                    with TabPane("History", id="history"):
                        yield DataTable(id="history-table", cursor_type="row")
                    with TabPane("Queue", id="queue"):
                        yield Static(id="queue-status")
                        yield DataTable(id="queue-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        """Fill tables from the last run and start the resource timer."""
        t = self.query_one("#jobs", DataTable)
        for col, key in (("Gate", "gate"), ("Job", "job"), ("Cat", "cat"), ("Status", "status"),
                         ("Time", "time"), ("Issues", "issues"), ("Note", "note")):
            t.add_column(col, key=key)
        h = self.query_one("#history-table", DataTable)
        for col in ("Run", "Started", "Status", "Commit", "Gates", "Time"):
            h.add_column(col)
        q = self.query_one("#queue-table", DataTable)
        for col in ("#", "State", "What", "Trust", "Title", "Run"):
            q.add_column(col)
        self._refresh_queue()
        self.set_interval(3.0, self._refresh_queue)
        self.set_interval(1.0, self._update_resources)
        self._show_subject()
        self._show_pending()
        if self.pick:
            self.action_open_build()

    def _show_subject(self) -> None:
        """Fill everything from the current pipeline and its last run."""
        self.sub_title = f"{self.p.name} · profile {self.profile} · {self.hw.summary()}"
        self.last_run = self.history.load("last")
        self._fill_jobs_table(self.last_run)
        self._refresh_history()
        self._update_status()
        self._update_resources()
        if self.last_run:
            self._show_report(self.last_run)
            self._select_first_failed()

    # ---------------------------------------------------------------- local builds
    def action_open_build(self) -> None:
        """`o`: choose a folder, archive or file on this computer to test."""
        if self.running:
            self.notify("Wait for the run to finish, or cancel it with x.", severity="warning")
            return
        here = Path(self.p.root) if self.p.gates else Path.cwd()
        back = self.project is not None and self.p is not self.project
        self.push_screen(OpenBuild(self.project, here.parent if here.parent != here else here, back), self._opened)

    def _opened(self, choice) -> None:
        if choice is None:
            return
        if choice == "project":
            pipeline, opts, note = self.project or _nothing(), {}, "the project"
        else:
            pipeline, note = choice.pipeline, choice.note
            opts = {"data_root": choice.data_root, "trusted": choice.trusted}
        try:
            self._use(pipeline, **opts)
        except (OSError, sqlite3.Error) as e:     # e.g. a folder we may read but not write to
            self._use(self.project or _nothing())
            self.notify(escape(f"It can't be opened: {e}"), severity="error", timeout=10)
            return
        finally:
            self.use_cache, self.mode_override, self.selected_job = True, None, None
            gates = self.query_one("#gates", SelectionList)
            gates.clear_options()
            gates.add_options([SLItem(f"{g.id}  [{g.mode}]", g.id, g.enabled) for g in self.p.gates])
            self.query_one("#logview", RichLog).clear()
            self.query_one("#report-md", Markdown).update("_Not run yet._")
            self._show_errors(None)
            self._show_subject()
            self._refresh_queue()
        self.action_tab("errors")
        self.notify(escape(f"{self.p.name}: {note}. Press r to run, d for a dry run."), timeout=8)

    def _show_pending(self) -> None:
        """Open on the Queue tab when commits wait for approval, the oldest one highlighted."""
        from .queue import PENDING, Queue
        t = self.query_one("#queue-table", DataTable)
        rows = [k.value for k in t.rows]
        pending = [str(i.id) for i in Queue(self.data_root).list((PENDING,), 40) if str(i.id) in rows]
        if not pending:
            return
        self.action_tab("queue")
        t.move_cursor(row=rows.index(pending[-1]))
        self.notify(f"{len(pending)} commit(s) waiting for approval: a = approve, n = reject", timeout=10)

    # ---------------------------------------------------------------- helpers
    def _selection(self) -> Selection:
        gates = list(self.query_one("#gates", SelectionList).selected)
        cats = list(self.query_one("#cats", SelectionList).selected)
        return Selection(gates=gates or None, categories=cats if len(cats) < 3 else None,
                         use_cache=self.use_cache, mode_override=self.mode_override,
                         reports=["md", "html", "pdf", "sarif", "junit"])

    @_while_mounted
    def _update_status(self) -> None:
        t = Text()
        t.append("cache ", style="bold")
        t.append("on" if self.use_cache else "off", style="green" if self.use_cache else "yellow")
        t.append("   mode ", style="bold")
        t.append(self.mode_override or "per gate")
        t.append("\n")
        if self.running:
            t.append("▶ running…", style="bold cyan")
        elif self.last_run:
            t.append("last run ")
            t.append_text(status_text(self.last_run.status))
            t.append(f"\n{self.last_run.run_id}", style="dim")
        self.query_one("#status", Static).update(t)

    @_while_mounted
    def _update_resources(self) -> None:
        u = live_usage()
        b = self.budget
        running = [k for k, v in self.row_status.items() if v == "running"]
        body = Group(
            Text.assemble(("CPU ", "bold"), _bar(u["cpu_percent"])),
            Text.assemble(("RAM ", "bold"), _bar(u["mem_percent"])),
            Text(f"{fmt_bytes(u['mem_available'])} free of {fmt_bytes(self.hw.mem_total)}", style="dim"),
            Text(f"parallel {len(running)}/{b.max_parallel} · kill at {b.kill_mem_percent:.0f}% RAM", style="dim"),
        )
        self.query_one("#resources", Static).update(body)

    def _row(self, gate: str, jid: str, cat: str, status: str, time: str = "", issues: str = "",
             note: str = "") -> list:
        red = status in (FAILED, ERROR)
        name = Text(jid, style="bold red" if red else "")
        iss = Text(issues, style="bold red" if red and issues not in ("", "0") else "")
        return [gate, name, cat, status_text(status), time, iss, Text(note, style="dim")]

    def _fill_jobs_table(self, run: RunResult | None, plan: dict[str, list[str]] | None = None) -> None:
        t = self.query_one("#jobs", DataTable)
        t.clear()
        self.results.clear()
        self.row_status.clear()
        if plan is not None:
            for gate, jobs in plan.items():
                for jid in jobs:
                    j = self.p.job(jid)
                    t.add_row(*self._row(gate, jid, j.category if j else "", "queued"), key=jid)
                    self.row_status[jid] = "queued"
            return
        if run is None:
            for g in self.p.gates:
                for j in self.p.jobs_in(g.id):
                    t.add_row(*self._row(g.id, j.id, j.category, "queued", note="not run yet"), key=j.id)
            return
        for g in run.gates:
            for r in g.jobs:
                self._set_result(r, add=True)

    @_while_mounted
    def _set_result(self, r: JobResult, add: bool = False) -> None:
        t = self.query_one("#jobs", DataTable)
        self.results[r.job_id] = r
        self.row_status[r.job_id] = r.status
        cells = self._row(r.gate, r.job_id, r.category, r.status, f"{r.duration:.1f}s",
                          str(len(r.active_errors)), r.note)
        if add or r.job_id not in t.rows:
            t.add_row(*cells, key=r.job_id)
            return
        for key, val in zip(("gate", "job", "cat", "status", "time", "issues", "note"), cells):
            t.update_cell(r.job_id, key, val, update_width=True)
        if r.job_id == self.selected_job:
            self._show_errors(r.job_id)

    @_while_mounted
    def _show_errors(self, jid: str | None) -> None:
        body = self.query_one("#errors-body", Static)
        if not jid:
            body.update(Text("Select a job to see its errors.", style="dim"))
            return
        r = self.results.get(jid)
        j = self.p.job(jid)
        parts = []
        if j:
            parts.append(Text.assemble((f"{j.title}", "bold"), f"  [{j.category}] gate {j.gate}"))
            parts.append(Text(f"$ {describe_command(j)}", style="dim"))
        if r is None:
            parts.append(Text("\nNot run yet.", style="dim"))
        else:
            parts.append(Text.assemble("\n", status_text(r.status), f"  {r.duration:.1f}s  exit {r.exit_code}  ",
                                       (r.note, "dim")))
            if not r.errors:
                parts.append(Text("\nNo issues. ✓", style="green"))
            for e in r.errors:
                if e.suppressed:
                    parts.append(Text(f"  suppressed: {e.location} {e.rule or ''} — {e.suppression_reason}", style="dim"))
                else:
                    parts.append(error_panel(e))
        body.update(Group(*parts))

    def _select_first_failed(self) -> None:
        t = self.query_one("#jobs", DataTable)
        for i, key in enumerate(t.rows):
            if self.row_status.get(key.value) in (FAILED, ERROR):
                t.move_cursor(row=i)
                self.selected_job = key.value
                self._show_errors(key.value)
                return
        if t.row_count:
            first = next(iter(t.rows)).value
            self.selected_job = first
            self._show_errors(first)

    def _show_report(self, run: RunResult) -> None:
        rp = Path(run.artifact_dir) / "report.md"
        md = rp.read_text(encoding="utf-8") if rp.exists() else "_No report for this run._"
        self.query_one("#report-md", Markdown).update(md)

    @_while_mounted
    def _refresh_history(self) -> None:
        h = self.query_one("#history-table", DataTable)
        h.clear()
        for r in self.history.list(50):
            h.add_row(r["id"], r["started"], status_text(r["status"]), (r["commit_sha"] or "")[:8],
                      r["gates"], f"{r['duration']:.1f}s", key=r["id"])

    def _write_log(self, text, style: str = "") -> None:
        self.query_one("#logview", RichLog).write(Text(text, style=style) if isinstance(text, str) else text)

    # ---------------------------------------------------------------- events
    def _event_from_thread(self, kind: str, data: dict) -> None:
        if kind == "resources":
            return
        self._from_thread(self._handle_event, kind, data)

    @_while_mounted
    def _handle_event(self, kind: str, d: dict) -> None:
        if kind == "run_start":
            self._fill_jobs_table(None, plan=d["jobs"])
            self._write_log(f"▶ run {d['run'].run_id} · commit {d['run'].commit[:8] or 'n/a'} · profile {d['run'].profile}",
                      "bold")
        elif kind == "gate_start":
            self._write_log(f"── gate {d['gate']} ({d['mode']}) ──", "bold magenta")
        elif kind == "job_start":
            self.row_status[d["job_id"]] = "running"
            self.query_one("#jobs", DataTable).update_cell(d["job_id"], "status", status_text("running"), update_width=True)
            self._write_log(f"▶ {d['job_id']} started", STYLE["running"])
        elif kind == "job_waiting":
            if d["job_id"] in self.row_status:
                self.query_one("#jobs", DataTable).update_cell(d["job_id"], "note", Text(d["reason"], style="yellow"), update_width=True)
        elif kind == "job_log":
            self._write_log(Text.assemble((f"{d['job_id']:>14} │ ", "dim"), d["line"]))
        elif kind == "job_end":
            r: JobResult = d["result"]
            self._set_result(r)
            self._write_log(Text.assemble(status_text(r.status), f" {r.job_id} {r.duration:.1f}s ",
                                    (f"{len(r.active_errors)} issue(s)" if r.active_errors else "", "red")))
        elif kind == "gate_end":
            for pr in d["gate"].policy:
                if not pr["skipped"]:
                    self._write_log(("✓ " if pr["passed"] else "✗ ") + pr["human"], "green" if pr["passed"] else "bold red")
        elif kind == "message":
            self._write_log(f"! {d['text']}", "bold red" if d.get("level") == "error" else "yellow")
            self.notify(escape(d["text"]), severity="error" if d.get("level") == "error" else "warning")

    @on(DataTable.RowHighlighted, "#jobs")
    @_while_mounted
    def _job_highlighted(self, ev: DataTable.RowHighlighted) -> None:
        if ev.row_key is not None:
            self.selected_job = ev.row_key.value
            self._show_errors(self.selected_job)

    @on(DataTable.RowSelected, "#history-table")
    @_while_mounted
    def _history_selected(self, ev: DataTable.RowSelected) -> None:
        run = self.history.load(ev.row_key.value)
        if run:
            self.last_run = run
            self._fill_jobs_table(run)
            self._show_report(run)
            self._update_status()
            self._select_first_failed()
            self.query_one("#tabs", TabbedContent).active = "errors"

    @_while_mounted
    def _refresh_queue(self) -> None:
        """Reload the approval queue (written by `gatewright watch` in another process)."""
        from .queue import Queue
        q = Queue(self.data_root)
        t = self.query_one("#queue-table", DataTable)
        keep = t.cursor_row
        t.clear()
        items = q.list(("pending", "approved", "running", "done", "rejected", "error"), 40)
        style = {"pending": "bold yellow", "approved": "cyan", "running": "bold cyan", "done": "green",
                 "rejected": "dim", "error": "bold red"}
        for i in items:
            trust = Text("trusted", style="green") if i.trusted else Text("untrusted" + (" (fork)" if i.from_fork else ""),
                                                                          style="bold red")
            t.add_row(str(i.id), Text(i.state, style=style.get(i.state, "")), i.label, trust, i.title[:40],
                      f"{i.run_id} {i.result}".strip(), key=str(i.id))
        if items and keep is not None and keep < len(items):
            t.move_cursor(row=keep)
        pending = sum(1 for i in items if i.state == "pending")
        alive = q.watcher_alive()
        self.query_one("#queue-status", Static).update(Text.assemble(
            ("watcher: ", "bold"), ("running", "green") if alive else ("not running (start `gatewright watch`)", "yellow"),
            f"   ·   {pending} waiting for approval   ·   a = approve, n = reject"))

    def _queue_selection(self):
        from .queue import Queue
        t = self.query_one("#queue-table", DataTable)
        if not t.row_count or t.cursor_row is None:
            self.notify("No queue item selected (open the Queue tab with u).", severity="warning")
            return None, None
        key = list(t.rows)[t.cursor_row].value
        return Queue(self.data_root), int(key)

    def action_approve(self) -> None:
        """`a`: approve the highlighted queue item (the watcher then runs that exact commit)."""
        q, item_id = self._queue_selection()
        if q is None:
            return
        try:
            item = q.approve(item_id, "tui")
            extra = "" if item.trusted else " (untrusted: runs without secrets)"
            self.notify(f"Approved #{item.id} {item.label}{extra}")
        except ValueError as e:
            self.notify(str(e), severity="error")
        self._refresh_queue()

    def action_reject(self) -> None:
        """`n`: reject the highlighted queue item."""
        q, item_id = self._queue_selection()
        if q is None:
            return
        try:
            item = q.reject(item_id, "tui", "rejected in the terminal UI")
            self.notify(f"Rejected #{item.id} {item.label}")
        except ValueError as e:
            self.notify(str(e), severity="error")
        self._refresh_queue()

    # ---------------------------------------------------------------- actions
    def action_tab(self, tab: str) -> None:
        """Switch the bottom tab (e / l / p keys)."""
        self.query_one("#tabs", TabbedContent).active = tab

    def action_toggle_cache(self) -> None:
        """`c`: use or ignore cached results."""
        self.use_cache = not self.use_cache
        self._update_status()
        self.notify(f"Cache {'on' if self.use_cache else 'off'}")

    def action_toggle_mode(self) -> None:
        """`m`: cycle gate mode override: per gate -> advisory -> enforced."""
        order = [None, "advisory", "enforced"]
        self.mode_override = order[(order.index(self.mode_override) + 1) % 3]
        self._update_status()
        self.notify(f"Gate mode: {self.mode_override or 'as configured per gate'}")

    def action_cancel(self) -> None:
        """`x`: cancel the running pipeline."""
        if self.running:
            self.engine.stop.set()
            self.notify("Cancelling run…", severity="warning")

    def action_dry_run(self) -> None:
        """`d`: show the plan in the Log tab."""
        if self.running:
            return
        try:
            plan = self.engine.plan(self._selection())
        except ValueError as e:
            self.notify(str(e), severity="error")
            return
        self.action_tab("log")
        self._write_log(f"DRY RUN · profile {plan.profile} · max {plan.budget['max_parallel']} parallel · "
                  f"{plan.budget['cpu']:g} CPU / {fmt_bytes(plan.budget['mem'])} RAM", "bold")
        for g in plan.gates:
            cov = f", coverage ≥ {g.coverage_min:g}%" if g.coverage_min is not None else ""
            self._write_log(f"gate {g.gate} ({g.mode}{cov})", "bold magenta")
            if g.blocked_reason:
                self._write_log(f"  ⏸ would be blocked: {g.blocked_reason}", "red")
            for i, wave in enumerate(g.waves, 1):
                for j in wave:
                    ok = tool_available(j, self.p.root)
                    mark, style = ("✓", "green") if ok else (("–", "yellow") if j.optional else ("✗", "bold red"))
                    self._write_log(Text.assemble(f"  wave {i}  ", (f"{mark} {j.id}", style),
                                            f"  [{j.category}] cpu {j.cpu:g} mem {fmt_bytes(j.mem)}  ",
                                            (describe_command(j), "dim")))
        for w in plan.warnings:
            self._write_log(f"! {w}", "yellow")

    def action_run(self) -> None:
        """`r`: run the selected gates/categories."""
        self._start(rerun=False)

    def action_rerun_failed(self) -> None:
        """`f`: re-run what failed in the last run."""
        if not self.last_run:
            self.notify("No previous run to resume.", severity="warning")
            return
        self._start(rerun=True)

    def _start(self, rerun: bool) -> None:
        if self.running:
            self.notify("A run is already in progress.", severity="warning")
            return
        sel = self._selection()
        if not sel.gates:
            self.notify("Select at least one gate.", severity="error")
            return
        self.running = True
        self._update_status()
        self.query_one("#logview", RichLog).clear()
        self.action_tab("log")
        self._worker(sel, self.last_run.run_id if rerun and self.last_run else None)

    @work(thread=True, exclusive=True)
    def _worker(self, sel: Selection, rerun_id: str | None) -> None:
        try:
            run = self.engine.rerun_failed(rerun_id) if rerun_id else self.engine.run(sel)
        except Exception as e:  # show, don't crash the UI
            self._from_thread(self._run_failed, str(e))
            return
        self._from_thread(self._run_done, run)

    def _from_thread(self, fn, *args) -> None:
        """Hand a result to the UI thread, unless the user already quit (then there is no UI)."""
        try:
            self.call_from_thread(fn, *args)
        except (RuntimeError, NoMatches):   # app stopped, or its widgets are being torn down
            pass

    def on_unmount(self) -> None:
        """Quitting during a run cancels it: no job keeps running without a UI to show it."""
        if self.running:
            self.engine.stop.set()

    @_while_mounted
    def _run_failed(self, msg: str) -> None:
        self.running = False
        self._update_status()
        self.notify(escape(f"Run could not start: {msg}"), severity="error", timeout=10)

    @_while_mounted
    def _run_done(self, run: RunResult) -> None:
        self.running = False
        self.last_run = run
        self._update_status()
        self._refresh_history()
        self._show_report(run)
        self._select_first_failed()
        self.action_tab("errors")
        sev = "information" if run.status in ("passed",) else "warning" if run.status == "warn" else "error"
        self.notify(escape(f"Run {run.status.upper()} · report: {Path(run.artifact_dir) / 'report.md'}"),
                    severity=sev, timeout=8)
