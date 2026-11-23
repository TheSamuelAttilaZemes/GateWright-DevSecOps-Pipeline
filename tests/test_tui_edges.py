# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Terminal UI paths beyond the happy run: reopening, history, cancel, warnings, bad input."""

import asyncio

from textual.widgets import DataTable, SelectionList, Static

from gatewright.config import load_pipeline
from gatewright.engine import Engine, Selection
from gatewright.models import JobError, JobResult
from gatewright.tui import GatewrightApp

from .conftest import BASIC_PIPELINE

from .conftest import PY  # noqa: E402

JOBS = f"""\
jobs:
  - {{id: ok, gate: commit, run: '{PY} -c "print(1)"', cache: false}}
  - {{id: bad, gate: commit, run: '{PY} -c "raise SystemExit(2)"', cache: false}}
  - {{id: slow, gate: test, run: '{PY} -c "import time; time.sleep(30)"', cache: false}}
"""
POLICY = BASIC_PIPELINE.replace("  - id: test\n    mode: enforced\n",
                                "  - id: test\n    mode: enforced\n    requires: [commit]\n") + \
    "policy:\n  - {id: few, gates: [commit], metric: issues.total, op: '<=', value: 0}\n"


def _run(coro):
    return asyncio.run(coro)


async def _wait_idle(app, pilot, limit=200):
    for _ in range(limit):
        await pilot.pause(0.1)
        if not app.running:
            return


def text_of(app, selector) -> str:
    from rich.console import Console
    w = app.query_one(selector, Static)
    content = getattr(w, "content", None)
    if content is None:
        content = w.renderable
    c = Console(record=True, width=150)
    c.print(content)
    return c.export_text()


def test_reopen_shows_last_run_and_history(project):
    root = project(POLICY, JOBS)
    Engine(load_pipeline(root)).run(Selection(gates=["commit"], reports=["md"]))

    async def scenario():
        app = GatewrightApp(load_pipeline(root))
        async with app.run_test(size=(150, 46)) as pilot:
            await pilot.pause(0.3)
            assert app.selected_job == "bad"                     # first failure selected on open
            app._show_errors(None)
            assert "Select a job" in text_of(app, "#errors-body")
            app._show_errors("slow")                            # in the pipeline, not in that run
            assert "Not run yet" in text_of(app, "#errors-body")
            h = app.query_one("#history-table", DataTable)
            app.action_tab("history")
            await pilot.pause(0.1)
            h.focus()
            h.move_cursor(row=0)
            await pilot.press("enter")                          # open that run again
            await pilot.pause(0.3)
            assert app.query_one("#tabs").active == "errors"
    _run(scenario())


def test_suppressed_error_and_event_messages(project):
    root = project(POLICY, JOBS)

    async def scenario():
        app = GatewrightApp(load_pipeline(root))
        async with app.run_test(size=(150, 46)) as pilot:
            await pilot.pause(0.2)
            r = JobResult("ok", "commit", "dev", "warn")
            e = JobError("ok", "lint", "low", "old", rule="E1", file="a.py", line=1)
            e.suppressed, e.suppression_reason = True, "legacy"
            r.errors = [e]
            app._set_result(r)                                   # update in place
            app.selected_job = "ok"
            app._set_result(r)
            assert "suppressed" in text_of(app, "#errors-body")
            from gatewright.models import GateResult
            g = GateResult(gate="commit", mode="enforced")
            g.policy = [{"id": "few", "human": "issues <= 0", "passed": False, "skipped": False},
                        {"id": "x", "human": "skip me", "passed": True, "skipped": True}]
            app._handle_event("gate_end", {"gate": g})
            app._handle_event("message", {"level": "error", "text": "RAM at 99%"})
            app._handle_event("message", {"level": "warning", "text": "slow disk"})
            app._handle_event("job_waiting", {"job_id": "ok", "reason": "cpu budget"})
            app._handle_event("job_waiting", {"job_id": "ghost", "reason": "x"})
            app._event_from_thread("resources", {})              # ignored (the timer shows resources)
    _run(scenario())


def test_toggles_cancel_and_guards(project):
    root = project(POLICY, JOBS)

    async def scenario():
        app = GatewrightApp(load_pipeline(root))
        async with app.run_test(size=(150, 46)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("c")
            assert app.use_cache is False
            await pilot.press("f")                               # no previous run: warns, does nothing
            assert not app.running
            app.action_cancel()                                  # not running: nothing to cancel
            app.query_one("#gates", SelectionList).deselect_all()
            app.action_run()                                     # no gates selected
            assert not app.running
            app.query_one("#gates", SelectionList).select_all()
            app.mode_override = "advisory"                       # let the test gate run after commit fails
            app.action_run()
            await pilot.pause(1.5)
            assert app.running
            app.action_run()                                     # already running: refused
            app.action_dry_run()                                 # ignored while running
            await pilot.press("x")                               # cancel
            await _wait_idle(app, pilot)
            return app.last_run.status, dict(app.row_status)

    status, rows = _run(scenario())
    assert status == "cancelled" and rows["slow"] == "skipped"


def test_dry_run_shows_blocked_gate_and_warnings(project):
    root = project(POLICY, JOBS + "  - {id: missing, gate: test, run: 'not-installed-xyz', optional: true}\n")

    async def scenario():
        app = GatewrightApp(load_pipeline(root))
        async with app.run_test(size=(150, 46)) as pilot:
            await pilot.pause(0.2)
            gates = app.query_one("#gates", SelectionList)
            gates.deselect("commit")                             # test requires commit, which isn't selected
            app.action_dry_run()
            await pilot.pause(0.2)
            gates.deselect_all()
            app.action_dry_run()                                 # invalid selection -> error toast
    _run(scenario())


def test_worker_failure_is_shown_not_raised(project, monkeypatch):
    root = project(POLICY, JOBS)

    async def scenario():
        app = GatewrightApp(load_pipeline(root))
        async with app.run_test(size=(150, 46)) as pilot:
            await pilot.pause(0.2)
            monkeypatch.setattr(app.engine, "run", lambda sel: (_ for _ in ()).throw(RuntimeError("disk full")))
            app.action_run()
            await _wait_idle(app, pilot)
            return app.running
    assert _run(scenario()) is False


def test_queue_actions_without_selection_and_on_decided_items(project):
    from gatewright.queue import Queue
    from gatewright.vcs import VCSEvent
    root = project(POLICY, JOBS)

    async def scenario():
        app = GatewrightApp(load_pipeline(root))
        async with app.run_test(size=(150, 46)) as pilot:
            await pilot.pause(0.2)
            app.action_approve()                                 # empty queue: warning only
            app.action_reject()
            q = Queue(str(root))
            item, _ = q.add(VCSEvent("push", "c" * 40, "main"), True, False)
            q.reject(item.id, "someone", "no")
            app._refresh_queue()
            app.query_one("#queue-table", DataTable).move_cursor(row=0)
            app.action_approve()                                 # already rejected -> error toast
            app.action_reject()
    _run(scenario())


def test_quitting_during_a_run_cancels_it(project):
    """Regression: quitting left the run going; its result then hit a UI that no longer existed."""
    root = project(POLICY, JOBS)
    holder = {}

    async def scenario():
        app = GatewrightApp(load_pipeline(root))
        holder["app"] = app
        async with app.run_test(size=(150, 46)) as pilot:
            await pilot.pause(0.2)
            app.mode_override = "advisory"
            app.action_run()
            await pilot.pause(1.5)
            assert app.running
        return app
    app = _run(scenario())
    assert app.engine.stop.is_set()                         # on_unmount cancelled the run
    app._from_thread(lambda: None)                          # after exit: ignored, no exception


def test_late_table_event_after_teardown_is_ignored(project):
    """Regression: a RowHighlighted event delivered while closing raised NoMatches."""
    root = project(POLICY, JOBS)

    async def scenario():
        app = GatewrightApp(load_pipeline(root))
        async with app.run_test(size=(150, 46)) as pilot:
            await pilot.pause(0.2)
            await app.query_one("#errors-body").parent.remove()   # what teardown does
            app._show_errors("ok")                                # must not raise
    _run(scenario())


def test_every_async_ui_update_survives_teardown(project):
    """Regression: timers/table events/the run thread hit removed widgets while closing (NoMatches)."""
    root = project(POLICY, JOBS)
    Engine(load_pipeline(root)).run(Selection(gates=["commit"], reports=["md"]))

    async def scenario():
        app = GatewrightApp(load_pipeline(root))
        async with app.run_test(size=(150, 46)) as pilot:
            await pilot.pause(0.2)
            run = app.last_run
            await app.screen.query("*").remove()                 # what closing the app does
            r = JobResult("ok", "commit", "dev", "passed")
            for call in (app._update_status, app._update_resources, app._refresh_queue, app._refresh_history,
                         lambda: app._set_result(r), lambda: app._show_errors("ok"),
                         lambda: app._handle_event("job_start", {"job_id": "ok"}),
                         lambda: app._run_done(run), lambda: app._run_failed("x"),
                         lambda: app._history_selected(type("E", (), {"row_key": type("K", (), {"value": "last"})()})())):
                assert call() is None
    _run(scenario())


def test_columns_grow_with_their_content(project):
    """Regression: BLOCKED was cut to BLOCKE and notes to a few letters, the width of the first content."""
    import asyncio
    from gatewright.config import load_pipeline
    from gatewright.tui import GatewrightApp
    root = project(BASIC_PIPELINE, "jobs:\n  - {id: bad, gate: commit, run: 'exit 1'}\n  - {id: later, gate: test, run: 'echo hi'}\n")

    async def scenario():
        app = GatewrightApp(load_pipeline(root))
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.press("r")
            for _ in range(150):
                await pilot.pause(0.1)
                if not app.running and app.last_run:
                    break
            await pilot.pause(0.5)
            cols = app.query_one("#jobs").columns
            return {k.value: c.get_render_width(app.query_one("#jobs")) for k, c in cols.items()}, app.row_status
    width, status = asyncio.run(scenario())
    assert status["later"] == "blocked"
    assert width["status"] >= len("⏸ BLOCKED") and width["note"] >= len("enforced gate `commit` failed")
