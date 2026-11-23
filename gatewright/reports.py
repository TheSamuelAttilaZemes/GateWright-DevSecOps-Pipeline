# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Reports and exports, all rendered from the same RunResult:

  * Markdown  - report.md (summary + every gate) and gate-<id>.md (one per gate),
                rendered with the Jinja2 templates MD_SUMMARY / MD_ONE_GATE below
  * HTML/PDF  - report.html built by `render_html`, styled by CSS, converted to
                PDF with WeasyPrint (optional dependency; failure is reported,
                never fatal)
  * SARIF     - results.sarif for code-scanning tools (GitHub, GitLab, IDEs)
  * JUnit XML - junit.xml: one testsuite per gate, one testcase per job

To add a format: write a function that takes a RunResult and returns text,
then add a branch to `write_reports()` and the format name to the CLI help.
"""

from __future__ import annotations

import html
import json
import os
import re
import xml.etree.ElementTree as ET  # nosec B405 - only used to build XML
from pathlib import Path

from jinja2 import Environment

from . import __version__
from .models import (BLOCKED, CACHED, CANCELLED, ERROR, FAILED, PASSED, REUSED, SEVERITIES, SKIPPED, WARN,
                     GateResult, JobError, JobResult, RunResult)

ICON = {PASSED: "✅", WARN: "⚠️", FAILED: "❌", ERROR: "⛔", BLOCKED: "⏸️", SKIPPED: "⏭️",
        CACHED: "♻️", REUSED: "↩️", CANCELLED: "⏹️"}
SEV_ICON = {"critical": "🟣", "high": "🔴", "medium": "🟠", "low": "🟡", "info": "🔵"}
LANG = {".py": "python", ".js": "javascript", ".ts": "typescript", ".yml": "yaml", ".yaml": "yaml",
        ".sh": "bash", ".go": "go", ".rs": "rust", ".java": "java", ".tf": "hcl", ".json": "json",
        "Dockerfile": "dockerfile"}

_STATUS_ORDER = {FAILED: 0, ERROR: 1, BLOCKED: 2, CANCELLED: 3, WARN: 4, PASSED: 5, CACHED: 6, REUSED: 7, SKIPPED: 8}


def lang_for(file: str | None) -> str:
    """Syntax-highlighting language for a file name (Markdown code fences)."""
    if not file:
        return ""
    base = os.path.basename(file)
    return LANG.get(base) or LANG.get(os.path.splitext(file)[1], "")


def sorted_jobs(jobs: list[JobResult]) -> list[JobResult]:
    """Jobs ordered failed -> error -> blocked -> warn -> passed -> ..."""
    return sorted(jobs, key=lambda j: _STATUS_ORDER.get(j.status, 9))


def _fmt_dur(s: float) -> str:
    return f"{s:.1f}s" if s < 60 else f"{int(s // 60)}m{int(s % 60):02d}s"


def gate_label(g: GateResult) -> str:
    """Status text for a gate, making clear that advisory failures don't block."""
    s = g.status
    if s == FAILED and g.mode == "advisory":
        return "failed (advisory - not blocking)"
    return s


_env = Environment(autoescape=False, trim_blocks=True, lstrip_blocks=True)  # nosec B701 - markdown output
_env.filters["dur"] = _fmt_dur
_env.globals.update(ICON=ICON, SEV_ICON=SEV_ICON, lang_for=lang_for, sorted_jobs=sorted_jobs,
                    gate_label=gate_label, SEVERITIES=SEVERITIES)

MD_ERROR = """\
{% macro err(e) -%}
**{{ SEV_ICON.get(e.severity, '') }} {{ e.severity|upper }}** · {{ 'INFRA' if e.is_infra else 'CODE' }} · {{ e.category }}{% if e.tool %} · {{ e.tool }}{% endif %}{% if e.rule %} `{{ e.rule }}`{% endif %}{% if e.location %} — `{{ e.location }}`{% endif %}


> {{ e.human }}

{% if e.hint %}**How to fix:** {{ e.hint }}

{% endif %}
{% if e.snippet %}
```{{ lang_for(e.file) }}
{% for n, text in e.snippet %}
{{ '>>' if n == e.line else '  ' }} {{ '%4d'|format(n) }} | {{ text }}
{% endfor %}
```

{% elif e.raw and e.is_infra %}
<details><summary>Raw output</summary>

```
{{ e.raw[-1500:] }}
```
</details>

{% endif %}
{% if e.suppressed %}_Suppressed: {{ e.suppression_reason }}_

{% elif e.suppression_reason %}_⚠️ {{ e.suppression_reason }}_

{% endif %}
{%- endmacro %}
"""

MD_GATE = MD_ERROR + """\
{% macro gate_section(g, level='##') %}
{{ level }} {{ ICON.get(g.status, '') }} Gate `{{ g.gate }}` — {{ gate_label(g) }}

Mode: **{{ g.mode }}** · Duration: {{ g.duration|dur }}{% if g.note %} · {{ g.note }}{% endif %}


| | Job | Category | Status | Time | Issues | Peak RAM | Note |
|---|---|---|---|---|---|---|---|
{% for j in sorted_jobs(g.jobs) %}
| {{ ICON.get(j.status, '') }} | `{{ j.job_id }}` | {{ j.category }} | {{ j.status }} | {{ j.duration|dur }} | {{ j.active_errors|length }}{% if j.errors|length > j.active_errors|length %} (+{{ j.errors|length - j.active_errors|length }} suppressed){% endif %} | {{ (j.peak_mem / 1048576)|round(0)|int }}MB | {{ j.note }} |
{% endfor %}

{% if g.policy %}
**Policy**

| | Rule | Result |
|---|---|---|
{% for p in g.policy %}
| {{ '⏭️' if p.skipped else ('✅' if p.passed else '❌') }} | `{{ p.id }}` | {{ p.human }} |
{% endfor %}

{% endif %}
{% for j in sorted_jobs(g.jobs) if j.active_errors %}
{{ level }}# {{ ICON.get(j.status, '') }} `{{ j.job_id }}` — {{ j.active_errors|length }} issue(s)

{% for e in j.active_errors %}
{{ err(e) }}
{% endfor %}
{% endfor %}
{% endmacro %}
"""

MD_SUMMARY = MD_GATE + """\
# {{ ICON.get(run.status, '') }} Gatewright Pipeline report — {{ run.pipeline }}

| | |
|---|---|
| **Status** | {{ run.status|upper }} |
| **Run** | `{{ run.run_id }}`{% if run.resumed_from %} (resumed from `{{ run.resumed_from }}`){% endif %} |
| **Commit** | `{{ run.commit[:12] or 'not a git repo' }}`{% if run.branch %} on `{{ run.branch }}`{% endif %}{% if run.dirty %} (uncommitted changes){% endif %} |
| **Started** | {{ run.started }} |
| **Duration** | {{ run.duration|dur }} |
| **Machine** | {{ run.hardware.get('hostname', '') }} · {{ run.hardware.get('distro', '') }} · profile `{{ run.profile }}` · {{ run.hardware.get('cpu_logical', '?') }} CPU · {{ ((run.hardware.get('mem_total', 0)) / 1073741824)|round(1) }} GB RAM |
| **Gatewright** | {{ version }} |

## Summary

| | Gate | Mode | Status | Jobs OK | Time |
|---|---|---|---|---|---|
{% for g in run.gates %}
| {{ ICON.get(g.status, '') }} | `{{ g.gate }}` | {{ g.mode }} | {{ gate_label(g) }} | {{ g.jobs|selectattr('ok')|list|length }}/{{ g.jobs|length }} | {{ g.duration|dur }} |
{% endfor %}

**Findings:** {% for s in SEVERITIES %}{{ SEV_ICON[s] }} {{ s }} {{ counts[s] }}{{ ' · ' if not loop.last }}{% endfor %}{% if suppressed %} · suppressed {{ suppressed }}{% endif %}


{% if diff %}
**Since previous run** (`{{ diff.prev_id }}`): {{ diff.new|length }} new, {{ diff.fixed|length }} fixed, {{ diff.unchanged|length }} unchanged.
{% for e in diff.new %}
- 🆕 {{ SEV_ICON.get(e.severity, '') }} `{{ e.location or e.job_id }}` {{ e.human[:140] }}
{% endfor %}
{% for e in diff.fixed %}
- ✔️ fixed: `{{ e.location or e.job_id }}` {{ e.human[:140] }}
{% endfor %}

{% endif %}
{% if run.policy %}
**Run policy**

| | Rule | Result |
|---|---|---|
{% for p in run.policy %}
| {{ '⏭️' if p.skipped else ('✅' if p.passed else '❌') }} | `{{ p.id }}` | {{ p.human }} |
{% endfor %}

{% endif %}
{% if run.warnings %}
**Warnings**

{% for w in run.warnings %}
- ⚠️ {{ w }}
{% endfor %}

{% endif %}
{% for g in run.gates %}
{{ gate_section(g) }}
{% endfor %}
{% if suppressed_list %}
## Suppressed findings

| Job | Location | Rule | Reason |
|---|---|---|---|
{% for e in suppressed_list %}
| `{{ e.job_id }}` | `{{ e.location }}` | {{ e.rule or '' }} | {{ e.suppression_reason }} |
{% endfor %}

{% endif %}
## Tool versions

| Tool | Version |
|---|---|
{% for t, v in run.tool_versions.items() %}
| {{ t }} | {{ v }} |
{% endfor %}
"""

MD_ONE_GATE = MD_GATE + """\
# Gatewright — gate `{{ g.gate }}` · run `{{ run.run_id }}`

Commit `{{ run.commit[:12] or 'n/a' }}` · {{ run.started }}

{{ gate_section(g) }}
"""


def _ctx(run: RunResult, diff: dict | None) -> dict:
    return {"run": run, "version": __version__, "counts": run.severity_counts(),
            "suppressed": sum(1 for e in run.all_errors() if e.suppressed),
            "suppressed_list": [e for e in run.all_errors() if e.suppressed], "diff": diff}


def render_markdown(run: RunResult, diff: dict | None = None) -> str:
    """The summary report (report.md) with every gate."""
    return _env.from_string(MD_SUMMARY).render(**_ctx(run, diff))


def render_gate_markdown(run: RunResult, gate: GateResult) -> str:
    """The report for a single gate (gate-<id>.md)."""
    return _env.from_string(MD_ONE_GATE).render(run=run, g=gate, version=__version__)


# ---------------------------------------------------------------------------
# HTML / PDF
# ---------------------------------------------------------------------------
CSS = """
@page { size: A4; margin: 16mm 14mm; @bottom-right { content: "Page " counter(page) " of " counter(pages); font-size: 8pt; color: #888; } }
body { font-family: "DejaVu Sans", Arial, sans-serif; font-size: 10pt; color: #1d1d1f; line-height: 1.4; }
h1 { font-size: 18pt; margin: 0 0 4pt 0; } h2 { font-size: 13pt; border-bottom: 1px solid #ddd; padding-bottom: 3pt; margin-top: 18pt; }
h3 { font-size: 11pt; margin: 12pt 0 4pt; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0 10pt; font-size: 9pt; }
th, td { border: 1px solid #e2e2e2; padding: 3pt 5pt; text-align: left; vertical-align: top; }
th { background: #f5f5f7; }
code { font-family: "DejaVu Sans Mono", monospace; font-size: 8.5pt; background: #f2f2f2; padding: 0 2pt; border-radius: 2pt; }
.pill { display: inline-block; padding: 1pt 6pt; border-radius: 8pt; font-size: 8pt; font-weight: bold; color: white; }
.s-passed, .s-cached, .s-reused { background: #1a7f37; } .s-warn { background: #b58105; } .s-failed { background: #cf222e; }
.s-error { background: #8b1a1a; } .s-blocked { background: #6e7781; } .s-skipped { background: #a0a0a0; }
.s-cancelled { background: #9a6700; }
.sev-critical { background: #6f1d8f; } .sev-high { background: #cf222e; } .sev-medium { background: #d4630a; }
.sev-low { background: #b58105; } .sev-info { background: #2f6feb; }
.err { border: 1px solid #f1aeb5; border-left: 4pt solid #cf222e; background: #fff5f5; padding: 6pt 8pt; margin: 6pt 0; page-break-inside: avoid; }
.err.infra { border-color: #e6c3a1; border-left-color: #8b1a1a; background: #fff8f0; }
.err .human { font-size: 10pt; margin: 3pt 0; } .err .hint { color: #1a5c2e; margin: 3pt 0; }
.err .meta { font-size: 8.5pt; color: #555; }
table.code { border: 1px solid #ddd; font-family: "DejaVu Sans Mono", monospace; font-size: 8pt; margin: 4pt 0 0; }
table.code td { border: none; padding: 0 5pt; white-space: pre-wrap; }
table.code td.n { color: #888; text-align: right; width: 28pt; background: #fafafa; }
table.code tr.hit td { background: #ffd7d5; color: #82071e; font-weight: bold; }
table.code tr.hit td.n { background: #cf222e; color: white; }
.raw { font-family: "DejaVu Sans Mono", monospace; font-size: 7.5pt; white-space: pre-wrap; background: #f6f6f6; padding: 4pt; }
.muted { color: #777; } .summary td.k { width: 22%; font-weight: bold; background: #fafafa; }
.row-failed td, .row-error td { background: #fff1f0; }
"""


def _pill(text: str, cls: str) -> str:
    return f'<span class="pill {cls}">{html.escape(text)}</span>'


def _ticks(text: str) -> str:
    """Escape, then turn `code` spans into <code>."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", html.escape(text))


def _err_html(e: JobError) -> str:
    h = html.escape
    meta = [_pill(e.severity.upper(), f"sev-{e.severity}"), "INFRA" if e.is_infra else "CODE", h(e.category)]
    if e.tool:
        meta.append(h(e.tool))
    if e.rule:
        meta.append(f"<code>{h(e.rule)}</code>")
    if e.location:
        meta.append(f"<code>{h(e.location)}</code>")
    out = [f'<div class="err{" infra" if e.is_infra else ""}"><div class="meta">{" · ".join(meta)}</div>',
           f'<div class="human">{_ticks(e.human)}</div>']
    if e.hint:
        out.append(f'<div class="hint"><b>How to fix:</b> {_ticks(e.hint)}</div>')
    if e.snippet:
        rows = "".join(f'<tr class="{"hit" if n == e.line else ""}"><td class="n">{n}</td><td>{h(t)}</td></tr>'
                       for n, t in e.snippet)
        out.append(f'<table class="code">{rows}</table>')
    elif e.raw and e.is_infra:
        out.append(f'<div class="raw">{h(e.raw[-1200:])}</div>')
    if e.suppression_reason and not e.suppressed:
        out.append(f'<div class="meta">⚠ {h(e.suppression_reason)}</div>')
    out.append("</div>")
    return "".join(out)


def _policy_html(rules: list[dict]) -> str:
    if not rules:
        return ""
    rows = "".join(
        f"<tr><td>{_pill('SKIP', 's-skipped') if p['skipped'] else (_pill('PASS', 's-passed') if p['passed'] else _pill('FAIL', 's-failed'))}</td>"
        f"<td><code>{html.escape(p['id'])}</code></td><td>{_ticks(p['human'])}</td></tr>" for p in rules)
    return f"<p><b>Policy</b></p><table><tr><th></th><th>Rule</th><th>Result</th></tr>{rows}</table>"


def render_html(run: RunResult, diff: dict | None = None) -> str:
    """Self-contained HTML report (also the source for the PDF)."""
    h = html.escape
    counts = run.severity_counts()
    parts = [f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>Gatewright Pipeline report {h(run.run_id)}</title>"
             f"<style>{CSS}</style></head><body>"]
    parts.append(f"<h1>Gatewright Pipeline report — {h(run.pipeline)} {_pill(run.status.upper(), 's-' + run.status)}</h1>")
    hw = run.hardware
    rows = [("Run", f"<code>{h(run.run_id)}</code>" + (f" (resumed from <code>{h(run.resumed_from)}</code>)" if run.resumed_from else "")),
            ("Commit", f"<code>{h(run.commit[:12] or 'not a git repo')}</code> {h(run.branch)}" + (" (uncommitted changes)" if run.dirty else "")),
            ("Started", h(run.started)), ("Duration", _fmt_dur(run.duration)),
            ("Machine", f"{h(hw.get('hostname', ''))} · {h(hw.get('distro', ''))} · profile {h(run.profile)} · {hw.get('cpu_logical', '?')} CPU · "
                        f"{hw.get('mem_total', 0) / 1024**3:.1f} GB RAM"),
            ("Findings", " ".join(_pill(f"{s} {counts[s]}", f"sev-{s}") for s in SEVERITIES))]
    parts.append("<table class='summary'>" + "".join(f"<tr><td class='k'>{k}</td><td>{v}</td></tr>" for k, v in rows) + "</table>")

    parts.append("<h2>Summary</h2><table><tr><th>Gate</th><th>Mode</th><th>Status</th><th>Jobs OK</th><th>Time</th></tr>")
    for g in run.gates:
        parts.append(f"<tr><td><code>{h(g.gate)}</code></td><td>{h(g.mode)}</td><td>{_pill(gate_label(g), 's-' + g.status)}</td>"
                     f"<td>{sum(1 for j in g.jobs if j.ok)}/{len(g.jobs)}</td><td>{_fmt_dur(g.duration)}</td></tr>")
    parts.append("</table>")
    if diff:
        parts.append(f"<p><b>Since previous run</b> (<code>{h(diff['prev_id'])}</code>): {len(diff['new'])} new, "
                     f"{len(diff['fixed'])} fixed, {len(diff['unchanged'])} unchanged.</p>")
    parts.append(_policy_html(run.policy))
    if run.warnings:
        parts.append("<p><b>Warnings</b></p><ul>" + "".join(f"<li>{h(w)}</li>" for w in run.warnings) + "</ul>")

    for g in run.gates:
        parts.append(f"<h2>Gate <code>{h(g.gate)}</code> {_pill(gate_label(g), 's-' + g.status)}</h2>"
                     f"<p class='muted'>Mode {h(g.mode)} · {_fmt_dur(g.duration)} {h(g.note)}</p>")
        parts.append("<table><tr><th>Job</th><th>Category</th><th>Status</th><th>Time</th><th>Issues</th><th>Peak RAM</th><th>Note</th></tr>")
        for j in sorted_jobs(g.jobs):
            sup = len(j.errors) - len(j.active_errors)
            parts.append(f"<tr class='row-{j.status}'><td><code>{h(j.job_id)}</code></td><td>{h(j.category)}</td>"
                         f"<td>{_pill(j.status, 's-' + j.status)}</td><td>{_fmt_dur(j.duration)}</td>"
                         f"<td>{len(j.active_errors)}{f' (+{sup} suppressed)' if sup else ''}</td>"
                         f"<td>{j.peak_mem / 1024**2:.0f}MB</td><td>{h(j.note)}</td></tr>")
        parts.append("</table>")
        parts.append(_policy_html(g.policy))
        for j in sorted_jobs(g.jobs):
            if j.active_errors:
                parts.append(f"<h3><code>{h(j.job_id)}</code> — {len(j.active_errors)} issue(s)</h3>")
                parts.extend(_err_html(e) for e in j.active_errors)

    sup = [e for e in run.all_errors() if e.suppressed]
    if sup:
        parts.append("<h2>Suppressed findings</h2><table><tr><th>Job</th><th>Location</th><th>Rule</th><th>Reason</th></tr>")
        parts.extend(f"<tr><td>{h(e.job_id)}</td><td><code>{h(e.location)}</code></td><td>{h(e.rule or '')}</td>"
                     f"<td>{h(e.suppression_reason)}</td></tr>" for e in sup)
        parts.append("</table>")
    if run.tool_versions:
        parts.append("<h2>Tool versions</h2><table><tr><th>Tool</th><th>Version</th></tr>" +
                     "".join(f"<tr><td>{h(t)}</td><td>{h(v)}</td></tr>" for t, v in run.tool_versions.items()) + "</table>")
    parts.append(f"<p class='muted'>Generated by Gatewright {__version__}</p></body></html>")
    return "".join(parts)


def write_pdf(html_text: str, path: Path) -> str | None:
    """Returns an error message, or None on success."""
    import contextlib
    import io
    try:
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            from weasyprint import HTML  # type: ignore
    except ImportError:
        return "PDF export needs WeasyPrint: pip install weasyprint. Markdown and HTML reports were still written."
    except Exception as e:  # missing system libs (pango/gobject)
        from .platform_info import detect, install_hint
        lib = str(e).split(":")[0]
        fix = install_hint("pango", detect()) or "install Pango for your OS"
        return (f"PDF export needs the Pango library ({lib}). Fix: {fix}. "
                "Markdown and HTML reports were still written.")
    try:
        HTML(string=html_text).write_pdf(str(path))
    except Exception as e:  # e.g. missing fonts, broken image in the HTML
        return f"PDF rendering failed: {e}"
    return None


# ---------------------------------------------------------------------------
# SARIF / JUnit
# ---------------------------------------------------------------------------
_SARIF_LEVEL = {"critical": "error", "high": "error", "medium": "warning", "low": "note", "info": "note"}


def export_sarif(run: RunResult) -> dict:
    """SARIF 2.1 export: one SARIF run per tool, suppressed findings marked as such."""
    by_tool: dict[str, list[JobError]] = {}
    for e in run.all_errors():
        if not e.is_infra:
            by_tool.setdefault(e.tool or e.job_id, []).append(e)
    runs = []
    for tool, errs in by_tool.items():
        rules = {}
        results = []
        for e in errs:
            rid = e.rule or e.category
            rules.setdefault(rid, {"id": rid, "shortDescription": {"text": rid}, "help": {"text": e.hint or ""}})
            res = {"ruleId": rid, "level": _SARIF_LEVEL.get(e.severity, "warning"),
                   "message": {"text": e.human}, "partialFingerprints": {"gatewright/v1": e.fingerprint},
                   "properties": {"severity": e.severity, "job": e.job_id, "category": e.category}}
            if e.file:
                loc = {"physicalLocation": {"artifactLocation": {"uri": e.file.replace(os.sep, "/")}}}
                if e.line:
                    loc["physicalLocation"]["region"] = {"startLine": e.line}
                res["locations"] = [loc]
            if e.suppressed:
                res["suppressions"] = [{"kind": "external", "justification": e.suppression_reason}]
            results.append(res)
        runs.append({"tool": {"driver": {"name": tool, "informationUri": "https://example.invalid/gatewright",
                                         "rules": list(rules.values())}}, "results": results})
    return {"$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0", "runs": runs}


def export_junit(run: RunResult) -> str:
    """JUnit XML export: gates as test suites, jobs as test cases."""
    suites = ET.Element("testsuites", name=f"gatewright {run.pipeline}", time=f"{run.duration:.2f}")
    for g in run.gates:
        failures = sum(1 for j in g.jobs if j.status == FAILED)
        errors = sum(1 for j in g.jobs if j.status in (ERROR, BLOCKED))
        skipped = sum(1 for j in g.jobs if j.status == SKIPPED)
        s = ET.SubElement(suites, "testsuite", name=g.gate, tests=str(len(g.jobs)), failures=str(failures),
                          errors=str(errors), skipped=str(skipped), time=f"{g.duration:.2f}")
        for j in g.jobs:
            tc = ET.SubElement(s, "testcase", classname=f"{g.gate}.{j.category}", name=j.job_id,
                               time=f"{j.duration:.2f}")
            text = "\n\n".join(f"[{e.severity}] {e.location} {e.human}\nFix: {e.hint}" for e in j.active_errors)
            if j.status == FAILED:
                ET.SubElement(tc, "failure", message=f"{len(j.active_errors)} issue(s)").text = text
            elif j.status in (ERROR, BLOCKED):
                ET.SubElement(tc, "error", message=j.note or j.status).text = text
            elif j.status == SKIPPED:
                ET.SubElement(tc, "skipped", message=j.note)
    ET.indent(suites)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(suites, encoding="unicode")


def write_reports(run: RunResult, out_dir: Path, formats: list[str], diff: dict | None = None) -> tuple[list[str], list[str]]:
    """Write requested formats. Returns (written files, warnings)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written, warnings = [], []
    formats = [f.strip().lower() for f in formats if f.strip()]
    if "md" in formats:
        p = out_dir / "report.md"
        p.write_text(render_markdown(run, diff), encoding="utf-8")
        written.append(str(p))
        for g in run.gates:
            gp = out_dir / f"gate-{g.gate}.md"
            gp.write_text(render_gate_markdown(run, g), encoding="utf-8")
            written.append(str(gp))
    if "html" in formats or "pdf" in formats:
        html_text = render_html(run, diff)
        hp = out_dir / "report.html"
        hp.write_text(html_text, encoding="utf-8")
        written.append(str(hp))
        if "pdf" in formats:
            pp = out_dir / "report.pdf"
            err = write_pdf(html_text, pp)
            if err:
                warnings.append(err)
            else:
                written.append(str(pp))
    if "sarif" in formats:
        p = out_dir / "results.sarif"
        p.write_text(json.dumps(export_sarif(run), indent=2), encoding="utf-8")
        written.append(str(p))
    if "junit" in formats:
        p = out_dir / "junit.xml"
        p.write_text(export_junit(run), encoding="utf-8")
        written.append(str(p))
    return written, warnings
