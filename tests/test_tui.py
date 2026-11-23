# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Drive the terminal UI headlessly with Textual's test pilot."""

import asyncio

from gatewright.config import load_pipeline
from gatewright.tui import GatewrightApp

from .conftest import BASIC_PIPELINE

JOBS = """\
    jobs:
      - {id: ok, gate: commit, run: "{PY} -c \\"print('fine')\\""}
      - {id: bad, gate: commit, run: "{PY} boom.py"}
      - {id: later, gate: test, run: "{PY} -c \\"print('later')\\""}
"""


def _run(coro):
    return asyncio.run(coro)


def test_tui_runs_pipeline_and_shows_errors(project):
    root = project(BASIC_PIPELINE, JOBS, files={"boom.py": "raise SystemExit('broken on purpose')\n"})

    async def scenario():
        app = GatewrightApp(load_pipeline(root))
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("d")                      # dry run -> plan in the log
            await pilot.pause(0.2)
            await pilot.press("r")                      # run
            for _ in range(150):
                await pilot.pause(0.1)
                if not app.running:
                    break
            assert app.last_run is not None
            statuses = dict(app.row_status)
            selected = app.selected_job                 # the Errors tab shows this job
            errors_text = " ".join(e.human for e in app.results[selected].errors)
            return app.last_run.status, statuses, (selected, errors_text)

    status, statuses, (selected, errors_text) = _run(scenario())
    assert status == "failed"
    assert statuses == {"ok": "passed", "bad": "failed", "later": "blocked"}
    assert selected == "bad"                            # the failed job is auto-selected
    assert "broken on purpose" in errors_text


def test_tui_mode_toggle_and_rerun(project):
    root = project(BASIC_PIPELINE, JOBS, files={"boom.py": "raise SystemExit('broken')\n"})

    async def scenario():
        app = GatewrightApp(load_pipeline(root))
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.press("m")                      # advisory: later gate runs too
            assert app.mode_override == "advisory"
            await pilot.press("r")
            for _ in range(150):
                await pilot.pause(0.1)
                if not app.running:
                    break
            first = dict(app.row_status)
            (root / "boom.py").write_text("print('fixed')\n")
            await pilot.press("f")                      # re-run failed
            for _ in range(150):
                await pilot.pause(0.1)
                if not app.running:
                    break
            return first, dict(app.row_status), app.last_run.resumed_from

    first, second, resumed = _run(scenario())
    assert first["later"] == "passed" and first["bad"] == "failed"
    assert second["bad"] == "passed" and second["ok"] == "reused" and resumed


def test_tui_queue_approve_and_reject(project):
    from gatewright.queue import Queue
    from gatewright.vcs import VCSEvent
    root = project(BASIC_PIPELINE, JOBS, files={"boom.py": "print('ok')\n"})
    q = Queue(str(root))
    first, _ = q.add(VCSEvent("pull_request", "a" * 40, "feat", "bob", 1, title="Add thing"), False, False)
    second, _ = q.add(VCSEvent("pull_request", "b" * 40, "fix", "eve", 2, from_fork=True), False, False)

    async def scenario():
        app = GatewrightApp(load_pipeline(root))
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.press("u")                      # Queue tab
            await pilot.pause(0.3)
            table = app.query_one("#queue-table")
            keys = [k.value for k in table.rows]
            table.move_cursor(row=keys.index(str(first.id)))
            await pilot.press("a")
            await pilot.pause(0.2)
            table.move_cursor(row=[k.value for k in table.rows].index(str(second.id)))
            await pilot.press("n")
            await pilot.pause(0.2)

    _run(scenario())
    assert q.get(first.id).state == "approved" and q.get(first.id).decided_by == "tui"
    assert q.get(second.id).state == "rejected"


def test_tui_opens_on_the_queue_when_commits_wait(project):
    from gatewright.queue import Queue
    from gatewright.vcs import VCSEvent
    root = project(BASIC_PIPELINE, JOBS, files={"boom.py": "print('ok')\n"})
    q = Queue(str(root))

    async def scenario(key):
        app = GatewrightApp(load_pipeline(root))
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.pause(0.5)
            tab = app.query_one("#tabs").active
            if key:
                await pilot.press(key)
                await pilot.pause(0.2)
            return tab

    assert _run(scenario("")) == "errors"               # nothing to approve: as before
    first, _ = q.add(VCSEvent("pull_request", "a" * 40, "feat", "bob", 1), False, False)
    second, _ = q.add(VCSEvent("pull_request", "b" * 40, "fix", "eve", 2), False, False)
    assert _run(scenario("")) == "queue"
    assert q.get(first.id).state == q.get(second.id).state == "pending"   # opening it decides nothing
    _run(scenario("a"))                                  # the oldest one is highlighted
    assert q.get(first.id).state == "approved" and q.get(second.id).state == "pending"


def test_tui_does_not_override_textual_internals():
    """Regression: GatewrightApp once defined `_log`, replacing Textual's own logging method."""
    import textual.app
    from gatewright.tui import GatewrightApp
    import inspect
    mine = {n for n, v in vars(GatewrightApp).items() if inspect.isfunction(v)}
    allowed = {"__init__", "compose", "on_mount"}          # meant to be overridden
    clashes = sorted(n for n in mine - allowed if hasattr(textual.app.App, n) and not n.startswith("action_"))
    assert clashes == [], f"these methods override textual.app.App: {clashes}"
