# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Rich console rendering shared by the CLI and the TUI: status styles,
red-highlighted code snippets and human error panels."""

from __future__ import annotations

import os

from rich.console import Group
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from .models import (BLOCKED, CACHED, CANCELLED, ERROR, FAILED, PASSED, REUSED, SKIPPED, WARN, JobError, RunResult)
from .reports import lang_for, sorted_jobs

PLAIN = bool(os.environ.get("NO_COLOR"))

STYLE = {PASSED: "bold green", WARN: "bold yellow", FAILED: "bold red", ERROR: "bold dark_orange",
         BLOCKED: "dim", SKIPPED: "dim", CACHED: "green", REUSED: "green", CANCELLED: "bold yellow", "running": "bold cyan",
         "queued": "dim", "waiting": "yellow"}
SYMBOL = {PASSED: "✓", WARN: "!", FAILED: "✗", ERROR: "⚠", BLOCKED: "⏸", SKIPPED: "–", CACHED: "♻",
          REUSED: "↩", CANCELLED: "■", "running": "▶", "queued": "·", "waiting": "…"}
LABEL = {PASSED: "PASS", WARN: "WARN", FAILED: "FAIL", ERROR: "INFRA", BLOCKED: "BLOCKED", SKIPPED: "SKIP",
         CACHED: "CACHED", REUSED: "REUSED", CANCELLED: "CANCELLED", "running": "RUN", "queued": "QUEUED", "waiting": "WAIT"}
SEV_STYLE = {"critical": "bold white on magenta", "high": "bold white on red", "medium": "bold black on dark_orange",
             "low": "black on yellow", "info": "white on blue"}


def status_text(status: str, with_label: bool = True) -> Text:
    """A coloured status label (✓ PASS, ✗ FAIL, ⚠ INFRA, ...). Plain when NO_COLOR is set."""
    s = f"{SYMBOL.get(status, '?')} {LABEL.get(status, status.upper())}" if with_label else SYMBOL.get(status, "?")
    return Text(s, style="" if PLAIN else STYLE.get(status, ""))


def code_snippet(e: JobError) -> Syntax | None:
    """The error's code snippet with line numbers and the failing line in red."""
    if not e.snippet:
        return None
    start = e.snippet[0][0]
    code = "\n".join(t for _, t in e.snippet)
    syn = Syntax(code, lang_for(e.file) or "text", line_numbers=True, start_line=start,
                 highlight_lines={e.line} if e.line else set(), theme="ansi_dark", word_wrap=True)
    if e.line and not PLAIN:
        syn.stylize_range("bold white on red", (e.line - start + 1, 0), (e.line - start + 2, 0))
    return syn


def error_panel(e: JobError) -> Panel:
    """A bordered panel for one JobError: severity, CODE/INFRA, location,
    human text, fix hint, red-highlighted code. Used by the CLI and the TUI.
    """
    head = Text()
    head.append(f" {e.severity.upper()} ", style="" if PLAIN else SEV_STYLE.get(e.severity, ""))
    head.append("  ")
    head.append("INFRA" if e.is_infra else "CODE", style="bold dark_orange" if e.is_infra else "bold red")
    head.append(f" · {e.category}")
    if e.tool:
        head.append(f" · {e.tool}")
    if e.rule:
        head.append(f" {e.rule}", style="bold")
    if e.location:
        head.append(f"  {e.location}", style="underline")
    body = [head, Text(""), Text(e.human, style="bold")]
    if e.hint:
        t = Text("How to fix: ", style="bold green")
        t.append(e.hint, style="green")
        body += [Text(""), t]
    syn = code_snippet(e)
    if syn:
        body += [Text(""), syn]
    elif e.is_infra and e.raw:
        body += [Text(""), Text(e.raw.strip()[-800:], style="dim")]
    if e.suppression_reason and not e.suppressed:
        body += [Text(f"⚠ {e.suppression_reason}", style="yellow")]
    body.append(Text(f"fingerprint {e.fingerprint}", style="dim"))
    color = "dark_orange" if e.is_infra else "red"
    return Panel(Group(*body), border_style=color, title=f"[{color}]{e.job_id}[/]", title_align="left")


def summary_table(run: RunResult) -> Table:
    """Table of every gate and job of a run, failures first."""
    t = Table(title=f"Run {run.run_id}", show_lines=False, expand=False)
    for c in ("Gate", "Mode", "Job", "Cat", "Status", "Time", "Issues", "Peak RAM", "Note"):
        t.add_column(c, no_wrap=c not in ("Note",))
    for g in run.gates:
        first = True
        for j in sorted_jobs(g.jobs):
            sup = len(j.errors) - len(j.active_errors)
            issues = Text(str(len(j.active_errors)), style="bold red" if j.active_errors and not PLAIN else "")
            if sup:
                issues.append(f" (+{sup} sup)", style="dim")
            gate_cell = Text.assemble(status_text(g.status, False), f" {g.gate}") if first else Text("")
            t.add_row(gate_cell, g.mode if first else "", Text(j.job_id, style="bold red" if j.status in (FAILED, ERROR) and not PLAIN else ""),
                      j.category, status_text(j.status), f"{j.duration:.1f}s", issues,
                      f"{j.peak_mem / 1024**2:.0f}MB" if j.peak_mem else "", Text(j.note, style="dim"))
            first = False
        if not g.jobs:
            t.add_row(Text.assemble(status_text(g.status, False), f" {g.gate}"), g.mode, "", "", status_text(g.status),
                      "", "", "", g.note)
        t.add_section()
    return t
