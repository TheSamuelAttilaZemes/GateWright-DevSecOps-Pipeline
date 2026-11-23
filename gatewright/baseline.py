# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Baselines and suppressions: accept known findings with a reason and an
expiry date, so old issues don't block new work - but don't get forgotten.

.gatewright/baseline.yml:
    suppressions:
      - fingerprint: 3f2a9c...        # exact match (from `gatewright show`)
        reason: "Test fixture, not a real password"
        expires: 2027-01-31
        owner: alice
      - rule: B101                    # or match by rule (+ optional file glob)
        file: "tests/*"
        reason: "asserts are fine in tests"
        expires: 2027-06-30
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml

from .config import load_yaml_text
from .models import JobError, RunResult


@dataclass
class Suppression:
    """One accepted finding: matched by fingerprint, or by rule (+ file glob, + job)."""
    reason: str
    expires: date | None
    fingerprint: str | None = None
    rule: str | None = None
    file: str | None = None
    job: str | None = None
    owner: str = ""

    def expired(self, today: date | None = None) -> bool:
        """True once the expiry date has passed - the finding is active again."""
        return bool(self.expires and self.expires < (today or date.today()))

    def matches(self, e: JobError) -> bool:
        """Does this suppression cover the error?"""
        if self.fingerprint:
            return self.fingerprint == e.fingerprint
        if not self.rule or self.rule != e.rule:
            return False
        if self.file and not fnmatch.fnmatch(e.file or "", self.file):
            return False
        if self.job and self.job != e.job_id:
            return False
        return True


class Baseline:
    """All suppressions from .gatewright/baseline.yml. Invalid entries are skipped with a warning."""
    def __init__(self, root: str):
        self.path = Path(root) / ".gatewright" / "baseline.yml"
        self.items: list[Suppression] = []
        self.warnings: list[str] = []
        self.expired_hits: list[tuple[Suppression, JobError]] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = load_yaml_text(self.path.read_text(encoding="utf-8")) or {}
        except Exception as e:  # yaml.YAMLError and friends: a broken baseline must not stop the pipeline
            self.warnings.append(f"baseline.yml could not be read ({e}); no suppressions applied")
            return
        entries = data.get("suppressions", []) if isinstance(data, dict) else None
        if not isinstance(entries, list):
            if entries is not None or not isinstance(data, dict):
                self.warnings.append("baseline.yml: expected `suppressions:` with a list of entries; ignored")
            return
        for i, s in enumerate(entries):
            if not isinstance(s, dict):
                self.warnings.append(f"baseline.yml suppressions[{i}]: must be a mapping (key: value); entry ignored")
                continue
            if not s.get("reason"):
                self.warnings.append(f"baseline.yml suppressions[{i}]: a `reason` is required; entry ignored")
                continue
            if not (s.get("fingerprint") or s.get("rule")):
                self.warnings.append(f"baseline.yml suppressions[{i}]: needs `fingerprint` or `rule`; entry ignored")
                continue
            exp = s.get("expires")
            if isinstance(exp, str):
                try:
                    exp = date.fromisoformat(exp)
                except ValueError:
                    self.warnings.append(f"baseline.yml suppressions[{i}]: bad date {exp!r}; use YYYY-MM-DD")
                    exp = None
            elif exp is not None and not isinstance(exp, date):
                self.warnings.append(f"baseline.yml suppressions[{i}]: bad date {exp!r}; use YYYY-MM-DD")
                exp = None
            self.items.append(Suppression(reason=s["reason"], expires=exp, fingerprint=s.get("fingerprint"),
                                          rule=s.get("rule"), file=s.get("file"), job=s.get("job"),
                                          owner=s.get("owner", "")))

    def apply(self, e: JobError) -> None:
        """Mark the error suppressed if a valid suppression matches. Infra errors
        are never suppressed. Expired matches are recorded so the run can warn.
        """
        if e.is_infra:
            return  # never suppress infrastructure problems
        for s in self.items:
            if s.matches(e):
                if s.expired():
                    self.expired_hits.append((s, e))
                    e.suppression_reason = f"suppression expired on {s.expires} ({s.reason})"
                    continue
                e.suppressed = True
                e.suppression_reason = s.reason + (f" (expires {s.expires})" if s.expires else "")
                return

    @staticmethod
    def write_from_run(root: str, run: RunResult, days: int, reason: str) -> tuple[Path, int]:
        """Add every active finding of `run` to baseline.yml with an expiry date.
        Returns (path, number added). Existing entries are kept.
        """
        path = Path(root) / ".gatewright" / "baseline.yml"
        data = load_yaml_text(path.read_text(encoding="utf-8")) if path.exists() else None
        data = data or {}
        items = data.get("suppressions", []) or []
        known = {s.get("fingerprint") for s in items}
        expires = (date.today() + timedelta(days=days)).isoformat()
        added = 0
        for e in run.all_errors():
            if e.is_infra or e.suppressed or e.fingerprint in known:
                continue
            items.append({"fingerprint": e.fingerprint, "rule": e.rule, "file": e.file, "job": e.job_id,
                          "reason": reason, "expires": expires, "note": e.human[:120]})
            known.add(e.fingerprint)
            added += 1
        data["suppressions"] = items
        path.write_text("# Accepted findings. Each needs a reason and should expire.\n" +
                        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return path, added
