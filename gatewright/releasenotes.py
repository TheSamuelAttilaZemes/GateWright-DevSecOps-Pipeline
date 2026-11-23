# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Release notes (v0.6): commits since the last release + what was tested.

Commits are grouped by Conventional Commits prefixes (feat:, fix:, perf:,
security:, docs:, ...). `!` after the type, or a "BREAKING CHANGE" footer, lists
the commit under Breaking changes. Anything else goes under Other changes.
If a run is given, a Quality section is added from its results: gates, tests,
coverage, findings and whether the artifact is signed.
"""

from __future__ import annotations

import re
import subprocess  # nosec B404 - fixed git argv
from datetime import date
from pathlib import Path

from .models import RunResult

SECTIONS = [  # (heading, conventional types)
    ("Breaking changes", ()),
    ("Features", ("feat", "feature")),
    ("Fixes", ("fix", "bugfix")),
    ("Security", ("sec", "security")),
    ("Performance", ("perf",)),
    ("Documentation", ("docs", "doc")),
    ("Maintenance", ("refactor", "test", "tests", "build", "ci", "chore", "style", "deps")),
    ("Other changes", ()),
]
_CC = re.compile(r"^(?P<type>[a-zA-Z]+)(\((?P<scope>[^)]+)\))?(?P<bang>!)?:\s*(?P<msg>.+)$")
SEP = "\x1f"
END = "\x1e"


def _git(root: str, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=60,  # nosec B603 B607
                       check=False)
    if p.returncode != 0:
        raise ValueError(p.stderr.strip() or f"git {' '.join(args)} failed")
    return p.stdout


def last_tag(root: str, before: str = "HEAD") -> str | None:
    """Most recent tag reachable from the parent of `before` (so a tag ON `before` isn't the start)."""
    try:
        return _git(root, "describe", "--tags", "--abbrev=0", f"{before}^").strip() or None
    except ValueError:
        return None


def _check_ref(ref: str | None) -> None:
    if ref is not None and (not ref or ref.startswith("-")):
        raise ValueError(f"`{ref}` is not a git ref (refs can't be empty or start with '-').")


def commits(root: str, from_ref: str | None, to_ref: str = "HEAD", limit: int = 500) -> list[dict]:
    _check_ref(from_ref)
    _check_ref(to_ref)   # a "ref" like --output=x would otherwise be read by git as an option
    rng = f"{from_ref}..{to_ref}" if from_ref else to_ref
    out = _git(root, "log", "--no-merges", f"--max-count={limit}",
               f"--format=%H{SEP}%s{SEP}%an{SEP}%ad{SEP}%b{END}", "--date=short", rng)
    result = []
    for rec in out.split(END):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, subject, author, day, body = (rec.split(SEP) + [""] * 5)[:5]
        result.append({"sha": sha, "subject": subject.strip(), "author": author, "date": day, "body": body})
    return result


def classify(c: dict) -> tuple[str, str]:
    """(section heading, display text) for one commit."""
    m = _CC.match(c["subject"])
    breaking = "BREAKING CHANGE" in c["body"] or bool(m and m.group("bang"))
    text = c["subject"]
    if m:
        text = (f"**{m.group('scope')}:** " if m.group("scope") else "") + m.group("msg")
    if breaking:
        return "Breaking changes", text
    if m:
        t = m.group("type").lower()
        for heading, types in SECTIONS:
            if t in types:
                return heading, text
    return "Other changes", text


def quality(run: RunResult) -> list[str]:
    m: dict[str, float] = {}
    for j in run.all_jobs():
        for k, v in j.metrics.items():
            # same rule as the pipeline's policies: the lowest percent counts, counters add up
            m[k] = (min(v, m[k]) if k in m else v) if k.endswith("percent") else m.get(k, 0) + v
    c = run.severity_counts()
    lines = [f"- Pipeline run `{run.run_id}` on commit `{run.commit[:12]}`: **{run.status.upper()}**"]
    lines.append("- Gates: " + ", ".join(f"{g.gate} {'✓' if g.ok else '✗'}" for g in run.gates))
    if "tests.total" in m:
        lines.append(f"- Tests: {int(m.get('tests.passed', 0))} passed, {int(m.get('tests.failed', 0))} failed, "
                     f"{int(m.get('tests.skipped', 0))} skipped")
    if "coverage.percent" in m:
        lines.append(f"- Coverage: {m['coverage.percent']:.1f}%")
    lines.append(f"- Open findings: critical {c['critical']}, high {c['high']}, medium {c['medium']}, low {c['low']}")
    signed = (Path(run.artifact_dir) / "checksums.sha256.sig").exists()
    lines.append(f"- Artifact: checksummed{', signed' if signed else ' (not signed)'}")
    return lines


def generate(root: str, from_ref: str | None = None, to_ref: str = "HEAD", run: RunResult | None = None,
             title: str = "") -> str:
    """Markdown release notes for from_ref..to_ref (default: since the last tag)."""
    _check_ref(to_ref)
    if from_ref is None:
        from_ref = last_tag(root, to_ref)
    items = commits(root, from_ref, to_ref)
    groups: dict[str, list[str]] = {h: [] for h, _ in SECTIONS}
    authors: list[str] = []
    for c in items:
        heading, text = classify(c)
        groups[heading].append(f"- {text} (`{c['sha'][:8]}`)")
        if c["author"] not in authors:
            authors.append(c["author"])
    span = f"{from_ref}..{to_ref}" if from_ref else f"everything up to {to_ref}"
    out = [f"# {title or 'Release notes'}", "", f"_{date.today().isoformat()} · {len(items)} commit(s) · {span}_", ""]
    for heading, _ in SECTIONS:
        if groups[heading]:
            out += [f"## {heading}", "", *groups[heading], ""]
    if not items:
        out += ["No changes.", ""]
    if run is not None:
        out += ["## Quality", "", *quality(run), ""]
    if authors:
        out += ["## Contributors", "", ", ".join(authors), ""]
    return "\n".join(out)
