# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Command line interface (`gatewright ...`). Every command is a thin layer over the
engine and the other modules: argument parsing, calling one function, and
printing the result with Rich.

To add a command:
  1. write `def cmd_name(args) -> int` (return the process exit code:
     0 ok, 1 failed, 2 usage/config error)
  2. register it in `build_parser()` with `sub.add_parser(...)` and
     `.set_defaults(fn=cmd_name)`
Keep logic out of here - put it in the engine or a module so the TUI can use it too.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from typing import NoReturn

from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from . import __version__
from .config import ConfigError, find_root, load_pipeline
from .models import CANCELLED, ERROR, FAILED, JobResult, fmt_bytes
from .render import error_panel, status_text, summary_table

console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)
PIPELINE_CHOICES = ("auto", "own", "project", "template")


def _csv(v: str | None) -> list[str] | None:
    return [x.strip() for x in v.split(",") if x.strip()] if v else None


def _load(args):
    from .local import state_dir
    root = find_root(args.dir)
    if state_dir().resolve() in root.parents:
        err_console.print("[red]✗[/] This folder only holds the results of a local build (history, show, report, diff "
                          "and verify work here). To test the build again: gatewright run --local PATH")
        sys.exit(2)
    try:
        return load_pipeline(args.dir)
    except ConfigError as e:
        _config_problems(e)


def _config_problems(e: ConfigError) -> NoReturn:
    err_console.print("[bold red]✗ The pipeline configuration has problems:[/]")
    for p in e.problems:
        err_console.print(f"  [red]•[/] {escape(p)}")
    sys.exit(2)


def _current_project(args):
    """The project Gatewright was started in, or None."""
    return _load(args) if (find_root(args.dir) / ".gatewright" / "pipeline.yml").exists() else None


def _subject(args):
    """(pipeline, Engine options) for `--local PATH`, else for the current project."""
    if getattr(args, "local", None) is None:
        return _load(args), {}
    from . import local
    try:
        mode = args.pipeline
        build = local.prepare(local.resolve(_absolute(args.local)), mode,
                              _current_project(args) if mode == "project" else None)
    except ConfigError as e:
        _config_problems(e)
    except (local.LocalError, OSError) as e:
        err_console.print(f"[red]✗[/] {escape(str(e))}")
        sys.exit(2)
    console.print(f"Testing {escape(str(build.path))} with {escape(build.note)}")
    console.print(f"[dim]Results: gatewright -C {escape(shown(build.data_root))} history[/]", soft_wrap=True)
    return build.pipeline, {"data_root": build.data_root, "trusted": build.trusted}


def shown(path: str) -> str:
    """A path to paste into a shell."""
    return f'"{path}"' if " " in path else path


def _absolute(text: str) -> str:
    """On the command line relative paths are fine (`--local .`)."""
    from .local import clean_text
    plain = clean_text(text)
    return str(Path(plain).absolute()) if plain and Path(plain).exists() else text


def _root(args) -> str:
    """The project root for commands that read project state. Refuses (exit 2) outside a
    project, so e.g. `gatewright history` in your home folder doesn't create a database there."""
    root = find_root(args.dir)
    if not (root / ".gatewright" / "pipeline.yml").exists():
        err_console.print(f"[red]✗[/] No Gatewright project here ({root}): no .gatewright/pipeline.yml in this "
                          "folder or a parent. Run `gatewright init` or use `-C <project>`.")
        sys.exit(2)
    return str(root)


def parse_coverage_args(values: list[str] | None) -> dict[str, float]:
    """`--coverage 85` (whole run) and `--coverage test=90` (one gate) -> {"": 85, "test": 90}."""
    out: dict[str, float] = {}
    for v in values or []:
        gate, sep, num = v.rpartition("=")
        if sep and not gate:
            raise ValueError(f"--coverage {v}: write GATE=PERCENT (e.g. test=80) or just PERCENT")
        try:
            pct = float(num)
        except ValueError:
            raise ValueError(f"--coverage {v}: `{num}` is not a number") from None
        if not 0 <= pct <= 100:
            raise ValueError(f"--coverage {v}: must be between 0 and 100")
        out[gate.strip()] = pct
    return out


def _selection(args):
    from .engine import Selection
    return Selection(gates=_csv(args.gates), categories=_csv(args.category), jobs=_csv(args.job),
                     use_cache=not args.no_cache, mode_override=args.mode,
                     reports=_csv(args.report) or ["md", "html", "sarif", "junit"],
                     coverage=parse_coverage_args(args.coverage))


# ---------------------------------------------------------------- commands
def cmd_init(args):
    """`gatewright init`: create a starter pipeline (templates.py)."""
    from .templates import init
    root = Path(args.dir).resolve()
    try:
        tpl, files = init(root, args.template, args.force)
    except (FileExistsError, ValueError) as e:
        err_console.print(f"[red]✗[/] {e}")
        return 2
    console.print(f"[green]✓[/] Created a [bold]{tpl}[/] pipeline in {root}")
    for f in files:
        console.print(f"  + {f}")
    console.print("\nNext: [bold]gatewright doctor[/] to check tools, then [bold]gatewright run --dry-run[/].")
    return 0


def cmd_validate(args):
    """`gatewright validate`: load the config and report every problem."""
    p = _load(args)
    console.print(f"[green]✓[/] Pipeline [bold]{p.name}[/] is valid: {len(p.gates)} gates, {len(p.jobs)} jobs, "
                  f"{len(p.policy)} policy rules.")
    return 0


def cmd_doctor(args):
    """`gatewright doctor`: tools, OS, Python and hardware checks (doctor.py)."""
    from .doctor import run_doctor
    from .hardware import detect, make_budget, pick_profile
    try:
        p = load_pipeline(args.dir)
    except ConfigError as e:
        p = None
        err_console.print(f"[yellow]! pipeline not loaded ({len(e.problems)} problem(s)); checking base tools only[/]")
    checks = run_doctor(p)
    t = Table(title="gatewright doctor")
    for c in ("", "Check", "Detail", "How to fix"):
        t.add_column(c)
    bad = 0
    for c in checks:
        if c.ok:
            mark = Text("✓", style="green")
        elif c.required:
            mark, bad = Text("✗", style="bold red"), bad + 1
        else:
            mark = Text("!", style="yellow")
        t.add_row(mark, c.name + ("" if c.required else " (optional)"), c.detail, "" if c.ok else c.hint)
    console.print(t)
    hw = detect(str(find_root(args.dir)))
    prof = pick_profile(hw, p.profile if p else "auto")
    b = make_budget(hw, prof, p.limits if p else {})
    console.print(f"Hardware: {hw.summary()}")
    console.print(f"Profile [bold]{prof}[/]: up to {b.max_parallel} parallel jobs, {b.cpu:g} CPU, "
                  f"{fmt_bytes(b.mem)} RAM for jobs; watchdog at {b.kill_mem_percent:.0f}% RAM.")
    if bad:
        console.print(f"[bold red]{bad} required check(s) failed.[/]")
    else:
        console.print("[green]Ready to run.[/]")
    return 1 if bad else 0


def cmd_hw(args):
    """`gatewright hw`: detected hardware and the resource budget."""
    from .hardware import detect, live_usage, make_budget, pick_profile
    p = None
    try:
        p = load_pipeline(args.dir)
    except ConfigError:
        pass
    hw = detect(str(find_root(args.dir)))
    if args.json:
        print(json.dumps(hw.to_dict(), indent=2))
        return 0
    u = live_usage()
    prof = pick_profile(hw, p.profile if p else "auto")
    b = make_budget(hw, prof, p.limits if p else {})
    t = Table(title="Hardware", show_header=False)
    t.add_column(style="bold")
    t.add_column()
    for k, v in [("OS", f"{hw.distro or hw.os} ({hw.arch}) · {hw.os}"), ("Family", hw.os_family), ("Host", hw.hostname),
                 ("CPU", f"{hw.cpu_logical} logical / {hw.cpu_physical} physical, load {hw.load_1m}"),
                 ("RAM", f"{fmt_bytes(hw.mem_total)} total, {fmt_bytes(hw.mem_available)} free ({u['mem_percent']:.0f}% used)"),
                 ("Disk free", fmt_bytes(hw.disk_free)),
                 ("GPU", ", ".join(g["name"] for g in hw.gpus) or "none detected"),
                 ("Container", "yes" if hw.in_container else "no"),
                 ("Battery", {None: "n/a", True: "on battery", False: "plugged in"}[hw.on_battery]),
                 ("Profile", prof), ("Max parallel", str(b.max_parallel)),
                 ("Job budget", f"{b.cpu:g} CPU, {fmt_bytes(b.mem)} RAM"),
                 ("Limits", f"start new jobs below {b.max_cpu_percent:.0f}% CPU / {b.max_mem_percent:.0f}% RAM; "
                            f"kill above {b.kill_mem_percent:.0f}% RAM")]:
        t.add_row(k, v)
    console.print(t)
    return 0


def cmd_plan(args):
    """`gatewright plan` / `gatewright run --dry-run`: show waves, resources and missing tools."""
    from .engine import Engine
    p, opts = _subject(args)
    eng = Engine(p, **opts)
    try:
        plan = eng.plan(_selection(args))
    except ValueError as e:
        err_console.print(f"[red]✗[/] {e}")
        return 2
    b = plan.budget
    console.print(f"[bold]Dry run[/] — nothing will be executed. Profile [bold]{plan.profile}[/], "
                  f"max {b['max_parallel']} parallel, budget {b['cpu']:g} CPU / {fmt_bytes(b['mem'])} RAM")
    if plan.coverage_min is not None:
        console.print(f"Whole run needs coverage ≥ {plan.coverage_min:g}%")
    console.print()
    from .runner import describe_command, tool_available
    for g in plan.gates:
        style = "bold red" if g.blocked_reason else "bold"
        cov = f", coverage ≥ {g.coverage_min:g}%" if g.coverage_min is not None else ""
        console.print(Text(f"Gate {g.gate} ({g.mode}{cov})", style=style))
        if g.blocked_reason:
            console.print(f"  [red]⏸ would be blocked:[/] {g.blocked_reason}")
        for i, wave in enumerate(g.waves, 1):
            console.print(f"  wave {i}:")
            for j in wave:
                avail = tool_available(j, p.root)
                tool = Text(f"  ✓ {j.tool_name}" if avail else (f"  – {j.tool_name} missing (optional, will skip)"
                            if j.optional else f"  ✗ {j.tool_name} MISSING"),
                            style="green" if avail else ("yellow" if j.optional else "bold red"))
                needs = f" needs {','.join(j.needs)}" if j.needs else ""
                console.print(Text.assemble(f"    • {j.id} [{j.category}] cpu {j.cpu:g} mem {fmt_bytes(j.mem)} "
                                            f"timeout {j.timeout}s{needs}", tool))
                if args.verbose:
                    console.print(f"        $ {describe_command(j)}", style="dim")
        console.print()
    for w in plan.warnings:
        console.print(f"[yellow]! {w}[/]")
    return 0


class _Progress:
    def __init__(self, verbose: bool):
        self.verbose = verbose

    def __call__(self, kind: str, d: dict) -> None:
        if kind == "run_start":
            r = d["run"]
            b = d["budget"]
            console.print(f"[bold]▶ Run {r.run_id}[/]  commit {r.commit[:8] or 'n/a'}  profile {r.profile}  "
                          f"(max {b.max_parallel} parallel)")
        elif kind == "gate_start":
            console.rule(f"gate [bold]{d['gate']}[/] ({d['mode']}) — {len(d['jobs'])} job(s)")
        elif kind == "job_start":
            console.print(f"  [cyan]▶[/] {d['job_id']}")
        elif kind == "job_waiting" and self.verbose:
            console.print(f"  [yellow]…[/] {d['job_id']} waiting: {d['reason']}", style="dim")
        elif kind == "job_log" and self.verbose:
            console.print(Text.assemble("    ", (f"{d['job_id']}│", "dim"), " ", d["line"]))
        elif kind == "job_end":
            r: JobResult = d["result"]
            extra = f" — {len(r.active_errors)} issue(s)" if r.active_errors else ""
            note = f" ({r.note})" if r.note else ""
            console.print(Text.assemble("  ", status_text(r.status), f" {r.job_id} {r.duration:.1f}s{extra}",
                                        (note, "dim")))
            if r.status in (FAILED, ERROR):
                first = r.active_errors[0] if r.active_errors else None
                if first:
                    console.print(Text(f"      → {first.human[:200]}", style="red"))
        elif kind == "gate_end":
            g = d["gate"]
            for pr in g.policy:
                if not pr["skipped"]:
                    console.print(Text(f"  {'✓' if pr['passed'] else '✗'} policy: {pr['human']}",
                                       style="green" if pr["passed"] else "bold red"))
        elif kind == "message":
            level = d.get("level")
            color = {"error": "red", "info": "green"}.get(level, "yellow")
            console.print(f"[{color}]{'✓' if level == 'info' else '!'} {d['text']}[/]")


def _print_result(run, show_errors: bool = True) -> None:
    console.print()
    console.print(summary_table(run))
    if show_errors:
        errs = [e for j in run.all_jobs() if j.status in (FAILED, ERROR) for e in j.active_errors]
        for e in errs[:30]:
            console.print(error_panel(e))
        if len(errs) > 30:
            console.print(f"... and {len(errs) - 30} more, see the report")
    for pr in run.policy:
        if not pr["skipped"]:
            console.print(Text(f"{'✓' if pr['passed'] else '✗'} run policy: {pr['human']}",
                               style="green" if pr["passed"] else "bold red"))
    for w in run.warnings:
        console.print(f"[yellow]! {escape(w)}[/]")
    c = run.severity_counts()
    console.print(f"Findings: critical {c['critical']} · high {c['high']} · medium {c['medium']} · low {c['low']}")
    console.print(Text.assemble("Result: ", status_text(run.status), f"   artifact: {run.artifact_dir}"))
    rep = Path(run.artifact_dir) / "report.md"
    if rep.exists():
        console.print(f"Report: {escape(str(rep))}")


def _install_sigint(engine):
    """First Ctrl+C stops the run cleanly, the second forces. Returns the previous
    handler so the caller can put it back (the CLI may be called in-process)."""
    def handler(sig, frame):
        if engine.stop.is_set():
            raise KeyboardInterrupt
        err_console.print("\n[yellow]Stopping... (Ctrl+C again to force)[/]")
        engine.stop.set()
    return signal.signal(signal.SIGINT, handler)


def cmd_run(args):
    """`gatewright run`: run the pipeline and print results; exit 1 if it failed."""
    if args.dry_run:
        return cmd_plan(args)
    from .engine import Engine
    p, opts = _subject(args)
    eng = Engine(p, _Progress(args.verbose), **opts)
    old = _install_sigint(eng)
    try:
        run = eng.run(_selection(args))
    except ValueError as e:
        err_console.print(f"[red]✗[/] {e}")
        return 2
    finally:
        signal.signal(signal.SIGINT, old)
    _print_result(run, not args.quiet)
    return {FAILED: 1, CANCELLED: 130}.get(run.status, 0)


def cmd_rerun(args):
    """`gatewright rerun-failed [run]`: re-run only what failed in a previous run."""
    from .engine import Engine
    p = _load(args)
    eng = Engine(p, _Progress(args.verbose))
    old = _install_sigint(eng)
    try:
        run = eng.rerun_failed(args.run_id)
    except ValueError as e:
        err_console.print(f"[red]✗[/] {e}")
        return 2
    finally:
        signal.signal(signal.SIGINT, old)
    _print_result(run)
    return {FAILED: 1, CANCELLED: 130}.get(run.status, 0)


def cmd_history(args):
    """`gatewright history`: list previous runs."""
    from .history import History
    h = History(_root(args))
    rows = h.list(args.n)
    if not rows:
        console.print("No runs yet. Start one with `gatewright run`.")
        return 0
    t = Table(title="Run history")
    for c in ("Run", "Started", "Status", "Commit", "Branch", "Gates", "Time", "Resumed from"):
        t.add_column(c)
    for r in rows:
        t.add_row(r["id"], r["started"], status_text(r["status"]), (r["commit_sha"] or "")[:8], r["branch"] or "",
                  r["gates"], f"{r['duration']:.1f}s", r["resumed_from"] or "")
    console.print(t)
    return 0


def cmd_diff(args):
    """`gatewright diff [old] [new]`: new and fixed findings between two runs."""
    from .history import History, diff
    h = History(_root(args))
    new = h.load(args.new or "last")
    old = h.load(args.old) if args.old else h.load("previous")
    if not new or not old:
        err_console.print("[red]✗[/] Need two runs to compare.")
        return 2
    d = diff(old, new)
    console.print(f"[bold]{old.run_id}[/] → [bold]{new.run_id}[/]: [red]{len(d['new'])} new[/], "
                  f"[green]{len(d['fixed'])} fixed[/], {len(d['unchanged'])} unchanged")
    for e in d["new"]:
        console.print(Text(f"  + [{e.severity}] {e.location or e.job_id}  {e.human[:150]}", style="red"))
    for e in d["fixed"]:
        console.print(Text(f"  - [{e.severity}] {e.location or e.job_id}  {e.human[:150]}", style="green"))
    return 0


def cmd_show(args):
    """`gatewright show [run]`: error panels with code highlighting."""
    from .history import History
    h = History(_root(args))
    run = h.load(args.run_id)
    if not run:
        err_console.print("[red]✗[/] Run not found.")
        return 2
    console.print(summary_table(run))
    for j in run.all_jobs():
        if args.job and j.job_id != args.job:
            continue
        errs = j.errors if args.all else j.active_errors
        for e in errs:
            console.print(error_panel(e))
    console.print(f"artifact: {run.artifact_dir}")
    return 0


def cmd_report(args):
    """`gatewright report [run]`: (re)generate reports in chosen formats."""
    from .history import History
    from .reports import write_reports
    root = _root(args)
    h = History(root)
    run = h.load(args.run_id)
    if not run:
        err_console.print("[red]✗[/] Run not found.")
        return 2
    # Never write into the artifact folder: that would break its checksums and signature.
    out = Path(args.out) if args.out else Path(root) / ".gatewright" / "reports" / run.run_id
    files, warns = write_reports(run, out, _csv(args.format) or ["md", "pdf"])
    for f in files:
        console.print(f"[green]✓[/] {f}")
    for w in warns:
        console.print(f"[yellow]! {w}[/]")
    return 0


def cmd_baseline(args):
    """`gatewright baseline create|list`: accept findings / list suppressions."""
    from .baseline import Baseline
    from .history import History
    root = _root(args)
    if args.action == "create":
        run = History(root).load(args.run_id)
        if not run:
            err_console.print("[red]✗[/] Run not found.")
            return 2
        path, n = Baseline.write_from_run(root, run, args.days, args.reason)
        console.print(f"[green]✓[/] Added {n} suppression(s) to {path} (expire in {args.days} days). "
                      "Review the file and give each a real reason.")
        return 0
    b = Baseline(root)
    for w in b.warnings:  # first: a broken baseline.yml must not look like "no suppressions"
        console.print(f"[yellow]! {w}[/]")
    if not b.items:
        console.print("No suppressions.")
        return 0
    t = Table(title="Baseline")
    for c in ("Match", "Reason", "Expires", "Owner"):
        t.add_column(c)
    for s in b.items:
        match = s.fingerprint or f"{s.rule} {s.file or ''}"
        exp = Text(str(s.expires or "never"), style="bold red" if s.expired() else "")
        t.add_row(match, s.reason, exp, s.owner)
    console.print(t)
    return 0


def cmd_verify(args):
    """`gatewright verify [run]`: check an artifact's checksums and, if signed, its signature."""
    from . import signing
    from .artifacts import verify
    from .history import History
    root = _root(args)
    run = History(root).load(args.run_id)
    if not run:
        err_console.print("[red]✗[/] Run not found.")
        return 2
    run_dir = Path(run.artifact_dir)
    problems = verify(run_dir)
    if problems:
        console.print(f"[bold red]✗ Artifact {run.run_id} failed verification:[/]")
        for p in problems:
            console.print(f"  [red]•[/] {p}")
        return 1
    console.print(f"[green]✓[/] Artifact {run.run_id} is intact (all checksums match).")
    try:
        cfg = signing.SigningConfig.from_dict(load_pipeline(root).signing)
    except ConfigError:
        cfg = signing.SigningConfig()
    signed = (run_dir / signing.SIG_FILE).exists()
    if signed or args.require_signature or cfg.required:
        ok, why = signing.verify(run_dir, cfg.allowed_signers_path(root))
        console.print(f"[green]✓[/] Signature: {why}" if ok else f"[bold red]✗ Signature: {why}[/]")
        if not ok:
            return 1
    else:
        console.print("[dim]  not signed (configure `signing:` to sign artifacts)[/]")
    return 0


def cmd_cache(args):
    """`gatewright cache-clear`: delete cached job results."""
    from .cache import Cache
    n = Cache(_root(args)).clear()
    console.print(f"Cleared {n} cached job result(s).")
    return 0


def cmd_clean(args):
    """`gatewright clean`: delete the files of old runs (never of deployed ones)."""
    from . import cleanup
    root = _root(args)
    old = cleanup.old_runs(root, args.keep, args.days)
    total = failed = 0
    for rid in old:
        size = cleanup.size_of(root, rid)
        if args.dry_run or cleanup.delete(root, rid):
            total += size
            console.print(f"  {'would delete' if args.dry_run else 'deleted'} {rid} ({fmt_bytes(size)})")
        else:
            failed += 1
            err_console.print(f"[red]✗[/] {rid} could not be deleted completely (a file is in use or protected)")
    console.print(f"{len(old) - failed} run(s), {fmt_bytes(total)}{' would be' if args.dry_run else ''} freed. "
                  "Deployed runs are always kept.")
    return 1 if failed else 0


def cmd_tui(args):
    """`gatewright tui`: open the terminal UI."""
    from .tui import GatewrightApp
    if args.local:   # a path: open it at once
        p, opts = _subject(args)
        GatewrightApp(p, project=_current_project(args), **opts).run()
        return 0
    p = _current_project(args)
    GatewrightApp(p, pick=p is None or args.local == "").run()
    return 0


# ---------------------------------------------------------------- v0.5: git integration
def _watcher(args):
    from .watch import Watcher
    from .notify import Notifier
    from .secrets import SecretStore
    root = str(find_root(args.dir))
    p = _load(args)
    notifier = Notifier(p.notify, SecretStore(root), lambda m: console.print(f"[yellow]{m}[/]")) if p.notify else None
    return Watcher(root, log=lambda m: console.print(m, markup=False, highlight=False), notifier=notifier)


def cmd_watch(args):
    """`gatewright watch`: poll/receive git host events, queue them, run approved commits."""
    try:
        w = _watcher(args)
    except Exception as e:  # missing vcs section, unreachable host, bad token secret...
        err_console.print(f"[red]✗[/] {e}")
        return 2
    def handler(sig, frame):
        console.print("\n[yellow]Stopping the watcher after the current run...[/]")
        w.stop.set()
    old = signal.signal(signal.SIGINT, handler)
    c = w.cfg
    console.print(f"[bold]Watching {c.provider}:{c.repo}[/] · branches {', '.join(c.branches)} · PRs "
                  f"{'on' if c.pull_requests else 'off'} · mode {c.mode} · poll every {c.poll_interval}s")
    try:
        w.loop(once=args.once)
    finally:
        signal.signal(signal.SIGINT, old)
    return 0


def cmd_queue(args):
    """`gatewright queue`: commits waiting for approval, running and done."""
    from .queue import Queue
    q = Queue(_root(args))
    items = q.list(None if args.all else ("pending", "approved", "running"), args.n)
    t = Table(title="Approval queue" + ("" if q.watcher_alive() else "  (no watcher running: start `gatewright watch`)"))
    for c in ("#", "State", "What", "Trust", "Title", "Created", "Run / note"):
        t.add_column(c)
    style = {"pending": "bold yellow", "approved": "cyan", "running": "bold cyan", "done": "green",
             "rejected": "dim", "superseded": "dim", "error": "bold red"}
    for i in items:
        t.add_row(str(i.id), Text(i.state, style=style.get(i.state, "")), i.label,
                  "trusted" if i.trusted else Text("untrusted" + (" (fork)" if i.from_fork else ""), style="red"),
                  i.title[:40], i.created, f"{i.run_id} {i.result}".strip() or i.note[:40])
    console.print(t if items else "The queue is empty.")
    return 0


def cmd_approve(args):
    """`gatewright approve <id>`: let the watcher run that exact commit."""
    from .queue import Queue
    try:
        item = Queue(_root(args)).approve(args.id, args.by or _user())
    except ValueError as e:
        err_console.print(f"[red]✗[/] {e}")
        return 2
    console.print(f"[green]✓[/] Approved #{item.id} {item.label}" +
                  ("" if item.trusted else " [yellow](untrusted: runs without secrets)[/]"))
    return 0


def cmd_reject(args):
    """`gatewright reject <id>`: never run that commit."""
    from .queue import Queue
    try:
        item = Queue(_root(args)).reject(args.id, args.by or _user(), args.reason or "")
    except ValueError as e:
        err_console.print(f"[red]✗[/] {e}")
        return 2
    console.print(f"Rejected #{item.id} {item.label}")
    return 0


def cmd_hook(args):
    """`gatewright hook install|uninstall|status`: the git pre-push hook."""
    from . import hooks
    root = str(find_root(args.dir))
    try:
        if args.action == "install":
            gates = _csv(args.gates) or ["commit"]
            try:
                known = {g.id for g in load_pipeline(root).gates}
            except ConfigError:
                known = set(gates)            # config is broken right now: the hook will report it on push
            unknown = [g for g in gates if g not in known]
            if unknown:
                raise ValueError(f"Unknown gate(s) {', '.join(unknown)}; pipeline.yml has: {', '.join(sorted(known))}.")
            path = hooks.install(root, gates, args.force)
            console.print(f"[green]✓[/] Installed {path}: `git push` now runs gate(s) {args.gates or 'commit'} first.")
        elif args.action == "uninstall":
            console.print(hooks.uninstall(root))
        else:
            console.print(f"pre-push hook: {hooks.status(root)}")
    except (ValueError, FileExistsError) as e:
        err_console.print(f"[red]✗[/] {e}")
        return 2
    return 0


# ---------------------------------------------------------------- v0.6: trust and release
def cmd_keygen(args):
    """`gatewright keygen`: create an Ed25519 signing key and print its allowed_signers line."""
    from . import signing
    path = Path(args.key).expanduser() if args.key else signing.default_key_path()
    try:
        line = signing.keygen(path, args.identity, args.passphrase)
    except signing.SigningError as e:
        err_console.print(f"[red]✗[/] {e}")
        return 2
    console.print(f"[green]✓[/] Created {path} (private, keep it safe) and {path}.pub")
    console.print("\nTrust this key by adding this line to .gatewright/allowed_signers (commit that file):")
    console.print(line, markup=False, highlight=False)
    if args.add:
        f = Path(_root(args)) / ".gatewright" / "allowed_signers"
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        console.print(f"[green]✓[/] Added to {f}")
    console.print(f"\nThen in pipeline.yml:\n  signing:\n    key: {path}\n    identity: {args.identity}")
    return 0


def cmd_sign(args):
    """`gatewright sign [run]`: sign an existing run's artifact."""
    from . import artifacts, signing
    from .history import History
    root = _root(args)
    run = History(root).load(args.run_id)
    if not run:
        err_console.print("[red]✗[/] Run not found.")
        return 2
    cfg = signing.SigningConfig.from_dict(_load(args).signing)
    run_dir = Path(run.artifact_dir)
    if artifacts.verify(run_dir):
        err_console.print("[red]✗[/] The artifact does not match its checksums; refusing to sign it.")
        return 1
    try:
        meta = signing.sign(run_dir, cfg)
    except signing.SigningError as e:
        err_console.print(f"[red]✗[/] {e}")
        return 2
    artifacts.pack(run_dir)
    console.print(f"[green]✓[/] Signed run {run.run_id} as {meta['identity']} ({meta['fingerprint']})")
    return 0


def _deployer(args):
    from .deploy import Deployer
    from .notify import Notifier
    from .secrets import SecretStore
    p = _load(args)
    notifier = Notifier(p.notify, SecretStore(p.root), lambda m: console.print(f"[yellow]{m}[/]")) if p.notify else None
    return Deployer(p, log=lambda m: console.print(m, markup=False, highlight=False), notifier=notifier)


def _print_deployment(d) -> int:
    ok = d.status == "succeeded"
    style = "bold green" if ok else "bold yellow" if d.status == "rolled_back" else "bold red"
    console.print(Text(f"{'✓' if ok else '✗'} {d.kind} #{d.id} of run {d.run_id} to {d.env}: {d.status}", style=style))
    if d.note:
        console.print(f"  {d.note}")
    console.print(f"  logs: {d.log_dir}")
    return 0 if ok else 1


def cmd_promote(args):
    """`gatewright promote [run] --to ENV`: deploy a tested artifact to an environment."""
    from .deploy import DeployError
    try:
        return _print_deployment(_deployer(args).promote(args.run_id, args.to, args.yes))
    except DeployError as e:
        err_console.print(f"[red]✗[/] {e}")
        return 2


def cmd_rollback(args):
    """`gatewright rollback ENV [--to RUN]`: redeploy the previous (or a given) artifact."""
    from .deploy import DeployError
    try:
        return _print_deployment(_deployer(args).rollback(args.env, args.to, args.yes))
    except DeployError as e:
        err_console.print(f"[red]✗[/] {e}")
        return 2


def cmd_deployments(args):
    """`gatewright deployments [ENV]`: deployment history, and what is live where."""
    d = _deployer(args)
    rows = d.deployments(args.env, args.n)
    if not rows:
        console.print("No deployments yet. Promote a run with `gatewright promote --to <environment>`.")
        return 0
    for env in sorted({r.env for r in rows}):
        cur = d.current(env)
        console.print(f"[bold]{env}[/]: live = {cur.run_id + ' (commit ' + cur.commit_sha[:8] + ')' if cur else 'nothing'}")
    t = Table(title="Deployments")
    for c in ("#", "Env", "Kind", "Run", "Commit", "Status", "When", "By", "Note"):
        t.add_column(c)
    for r in rows:
        st = Text(r.status, style="green" if r.status == "succeeded" else "yellow" if r.status == "rolled_back" else "red")
        t.add_row(str(r.id), r.env, r.kind, r.run_id, r.commit_sha[:8], st, r.started, r.by, r.note[:50])
    console.print(t)
    return 0


def cmd_release_notes(args):
    """`gatewright release-notes`: commits since the last release + the run's quality summary."""
    from .history import History
    from .releasenotes import generate
    root = _root(args) if args.run else str(find_root(args.dir))
    run = History(root).load(args.run) if args.run else None
    if args.run and not run:
        err_console.print(f"[red]✗[/] Run {args.run} not found (see `gatewright history`).")
        return 2
    to = args.to or (run.commit if run and run.commit else "HEAD")
    try:
        text = generate(root, args.from_ref, to, run, title=args.title or "")
    except ValueError as e:
        err_console.print(f"[red]✗[/] {e}")
        return 2
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        console.print(f"[green]✓[/] Wrote {args.output}")
    else:
        print(text)
    return 0


def cmd_notify_test(args):
    """`gatewright notify-test`: send a test message to every configured channel."""
    from .notify import Notifier
    from .secrets import SecretStore
    p = _load(args)
    if not p.notify.get("channels"):
        err_console.print("[red]✗[/] No `notify.channels` in pipeline.yml.")
        return 2
    n = Notifier({**p.notify, "on": ["test"], "channels": [{**c, "on": ["test"]} for c in p.notify["channels"]]},
                 SecretStore(p.root))
    n.send("test", "Gatewright: test notification", f"If you can read this, notifications from {p.name} work.")
    for kind, _ in n.sent:
        console.print(f"[green]✓[/] {kind}")
    for w in n.warnings:
        console.print(f"[red]✗[/] {w}")
    return 1 if n.warnings else 0


def _user() -> str:
    import getpass
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def build_parser() -> argparse.ArgumentParser:
    """All commands and options. Add new subcommands here."""
    ap = argparse.ArgumentParser(prog="gatewright", description="Self-hosted, hardware-aware DevSecOps pipeline runner.")
    ap.add_argument("--version", action="version", version=f"gatewright {__version__}")
    ap.add_argument("-C", "--dir", default=".", help="project directory (default: current)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create a starter pipeline")
    s.add_argument("--template", choices=["python", "node", "docker"])
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    sub.add_parser("validate", help="check the pipeline configuration").set_defaults(fn=cmd_validate)
    sub.add_parser("doctor", help="check that required tools are installed").set_defaults(fn=cmd_doctor)
    s = sub.add_parser("hw", help="show detected hardware and resource budget")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_hw)

    def sel_args(s):
        s.add_argument("-g", "--gates", help="comma list of gates (default: enabled gates)")
        s.add_argument("-c", "--category", help="comma list: dev,sec,ops")
        s.add_argument("-j", "--job", help="comma list of job ids (dependencies are included)")
        s.add_argument("--no-cache", action="store_true", help="ignore cached results")
        s.add_argument("--mode", choices=["enforced", "advisory"], help="override every gate's mode")
        s.add_argument("--report", help="report formats: md,html,pdf,sarif,junit (default md,html,sarif,junit)")
        s.add_argument("-v", "--verbose", action="store_true", help="stream job output")
        s.add_argument("--coverage", action="append", metavar="[GATE=]PERCENT",
                       help="minimum coverage for this run: 85 (whole run) or test=90 (one gate); repeatable; "
                            "overrides `coverage:` in pipeline.yml")
        s.add_argument("--local", metavar="PATH", help="test a folder, archive or file on this computer instead")
        s.add_argument("--pipeline", choices=PIPELINE_CHOICES, default="auto",
                       help="with --local: its own pipeline, the current project's, or a template "
                            "(auto: its own if it has one, else a template)")

    s = sub.add_parser("run", help="run the pipeline")
    sel_args(s)
    s.add_argument("--dry-run", action="store_true", help="show what would run, without running")
    s.add_argument("-q", "--quiet", action="store_true", help="don't print error details")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("plan", help="same as run --dry-run")
    sel_args(s)
    s.set_defaults(fn=cmd_plan)

    s = sub.add_parser("rerun-failed", aliases=["resume"], help="re-run only failed jobs of a run")
    s.add_argument("run_id", nargs="?", help="run id (default: last)")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_rerun)

    s = sub.add_parser("history", help="list previous runs")
    s.add_argument("-n", type=int, default=15)
    s.set_defaults(fn=cmd_history)

    s = sub.add_parser("diff", help="compare findings between two runs")
    s.add_argument("old", nargs="?")
    s.add_argument("new", nargs="?")
    s.set_defaults(fn=cmd_diff)

    s = sub.add_parser("show", help="show errors of a run with code highlighting")
    s.add_argument("run_id", nargs="?", default="last")
    s.add_argument("--job")
    s.add_argument("--all", action="store_true", help="include suppressed findings")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("report", help="(re)generate reports for a run")
    s.add_argument("run_id", nargs="?", default="last")
    s.add_argument("-f", "--format", help="md,html,pdf,sarif,junit (default md,pdf)")
    s.add_argument("-o", "--out", help="output directory (default: .gatewright/reports/<run>, outside the artifact)")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("baseline", help="accept current findings / list suppressions")
    s.add_argument("action", choices=["create", "list"])
    s.add_argument("run_id", nargs="?", default="last")
    s.add_argument("--days", type=int, default=90)
    s.add_argument("--reason", default="accepted at baseline creation - review")
    s.set_defaults(fn=cmd_baseline)

    s = sub.add_parser("verify", help="verify an artifact's checksums (and signature)")
    s.add_argument("run_id", nargs="?", default="last")
    s.add_argument("--require-signature", action="store_true", help="fail if the artifact is not signed")
    s.set_defaults(fn=cmd_verify)

    s = sub.add_parser("cache-clear", help="delete cached job results")
    s.set_defaults(fn=cmd_cache)

    s = sub.add_parser("clean", help="delete the files of old runs")
    s.add_argument("--keep", type=int, default=20, help="keep the newest N runs (default 20)")
    s.add_argument("--days", type=int, default=0, help="only delete runs older than this")
    s.add_argument("--dry-run", action="store_true", help="only show what would be deleted")
    s.set_defaults(fn=cmd_clean)

    s = sub.add_parser("tui", help="open the terminal UI")
    s.add_argument("--local", nargs="?", const="", metavar="PATH",
                   help="test a build on this computer (without PATH: choose one)")
    s.add_argument("--pipeline", choices=PIPELINE_CHOICES, default="auto", help="what to test it with (see run --help)")
    s.set_defaults(fn=cmd_tui)

    # v0.5 git integration
    s = sub.add_parser("watch", help="watch the git host, queue commits, run approved ones")
    s.add_argument("--once", action="store_true", help="one poll + run approved items, then exit (cron)")
    s.set_defaults(fn=cmd_watch)
    s = sub.add_parser("queue", help="show the approval queue")
    s.add_argument("--all", action="store_true", help="include finished/rejected/superseded items")
    s.add_argument("-n", type=int, default=30)
    s.set_defaults(fn=cmd_queue)
    s = sub.add_parser("approve", help="approve a queued commit")
    s.add_argument("id", type=int)
    s.add_argument("--by", help="name to record (default: your user name)")
    s.set_defaults(fn=cmd_approve)
    s = sub.add_parser("reject", help="reject a queued commit")
    s.add_argument("id", type=int)
    s.add_argument("--reason")
    s.add_argument("--by")
    s.set_defaults(fn=cmd_reject)
    s = sub.add_parser("hook", help="install/uninstall the git pre-push hook")
    s.add_argument("action", choices=["install", "uninstall", "status"])
    s.add_argument("-g", "--gates", help="gates to run before push (default: commit)")
    s.add_argument("--force", action="store_true", help="replace an existing pre-push hook (it is backed up)")
    s.set_defaults(fn=cmd_hook)

    # v0.6 trust and release
    s = sub.add_parser("keygen", help="create a signing key")
    s.add_argument("--identity", required=True, help="who signs, e.g. your e-mail address")
    s.add_argument("--key", help="key path (default: ~/.config/gatewright/signing_key)")
    s.add_argument("--add", action="store_true", help="also add the public key to .gatewright/allowed_signers")
    s.add_argument("--passphrase", action="store_true", help="protect the key with a passphrase (use ssh-agent to sign)")
    s.set_defaults(fn=cmd_keygen)
    s = sub.add_parser("sign", help="sign a run's artifact")
    s.add_argument("run_id", nargs="?", default="last")
    s.set_defaults(fn=cmd_sign)
    s = sub.add_parser("promote", help="deploy a run's artifact to an environment")
    s.add_argument("run_id", nargs="?", default="last")
    s.add_argument("--to", required=True, help="environment name")
    s.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    s.set_defaults(fn=cmd_promote)
    s = sub.add_parser("rollback", help="redeploy the previous artifact of an environment")
    s.add_argument("env")
    s.add_argument("--to", help="run id to roll back to (default: the one before the current)")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(fn=cmd_rollback)
    s = sub.add_parser("deployments", help="deployment history and what is live")
    s.add_argument("env", nargs="?")
    s.add_argument("-n", type=int, default=30)
    s.set_defaults(fn=cmd_deployments)
    s = sub.add_parser("release-notes", help="generate release notes from git + a run")
    s.add_argument("--from", dest="from_ref", help="start ref (default: last tag)")
    s.add_argument("--to", help="end ref (default: the run's commit, or HEAD)")
    s.add_argument("--run", help="run id for the quality section (e.g. last)")
    s.add_argument("--title")
    s.add_argument("-o", "--output", help="write to a file instead of printing")
    s.set_defaults(fn=cmd_release_notes)
    sub.add_parser("notify-test", help="send a test notification to every channel").set_defaults(fn=cmd_notify_test)
    return ap


def _lenient_output() -> None:
    """Print `?` for symbols the output's encoding doesn't have instead of crashing. Windows uses
    a legacy code page (cp1252, ...) when the output goes to a file or a pipe."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


def main(argv: list[str] | None = None) -> int:
    """Entry point of the `gatewright` command."""
    _lenient_output()
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args) or 0
    except KeyboardInterrupt:
        err_console.print("[red]Interrupted.[/]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
