# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Core data model. Every result in Gatewright is a structured object so that the
CLI, the TUI, reports and exports all read from the same data.

Configuration side (loaded by config.py):
    Pipeline -> Gate[]  (ordered stages, enforced or advisory)
             -> Job[]   (a command, its gate, parser, resources, ...)
             -> PolicyRule[]

Result side (produced by engine.py / runner.py):
    RunResult -> GateResult[] -> JobResult[] -> JobError[]

A JobError is *one problem* with a plain-language `human` text, a `hint`,
file/line, a code `snippet` and a `fingerprint` (stable id used by baselines
and run-to-run diffs). Its `category` decides whether it is a CODE problem
(your code is wrong) or an INFRA problem (the tooling/machine is wrong).

All result classes have to_dict()/from_dict() so they round-trip through
manifest.json and the SQLite history unchanged.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any

SEVERITIES = ["critical", "high", "medium", "low", "info"]
SEVERITY_RANK = {s: i for i, s in enumerate(reversed(SEVERITIES))}  # info=0 .. critical=4

# Job statuses
PASSED = "passed"        # exit 0 / all issues suppressed or below threshold
WARN = "warn"            # passed, but with non-blocking issues
FAILED = "failed"        # the code is bad (test failure, finding)
ERROR = "error"          # infrastructure problem (tool missing, timeout, OOM, crash)
SKIPPED = "skipped"      # not selected, optional tool missing
BLOCKED = "blocked"      # a dependency or enforced gate failed
CACHED = "cached"        # inputs unchanged since a passing run
REUSED = "reused"        # resumed run: result taken from previous run
CANCELLED = "cancelled"  # run level only: stopped by the user before it finished (never promotable)

OK_STATUSES = {PASSED, WARN, CACHED, REUSED, SKIPPED}

# Error categories. "code" kinds mean your code is wrong; "infra" kinds mean
# the pipeline/tooling is wrong. They are shown differently so you don't chase
# the wrong problem.
CODE_CATEGORIES = {"finding", "test_failure", "lint", "policy", "job_failure", "vulnerability", "secret"}
INFRA_CATEGORIES = {"tool_missing", "timeout", "resource", "config", "crash", "infra"}


def parse_mem(value: Any) -> int:
    """'512MB' / '2GB' / 1024 (MB) -> bytes."""
    if value is None:
        return 256 * 1024**2
    if isinstance(value, (int, float)):
        return int(value * 1024**2)
    s = str(value).strip().upper().replace(" ", "")
    units = {"K": 1024, "KB": 1024, "M": 1024**2, "MB": 1024**2, "G": 1024**3, "GB": 1024**3}
    for suffix in sorted(units, key=len, reverse=True):
        if s.endswith(suffix):
            return int(float(s[: -len(suffix)]) * units[suffix])
    return int(float(s) * 1024**2)


_CONTROL = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"        # OSC: window title, clipboard, links
                      r"|\x1b\[[0-?]*[ -/]*[@-~]"                  # CSI: colours, cursor, clear screen
                      r"|\x1b[ -~]?|[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # other escapes and control characters


def plain(text: str) -> str:
    """Text from tools and tested code without anything a terminal would act on."""
    return _CONTROL.sub("", text)


def fmt_bytes(n: float) -> str:
    """Format bytes for humans: 1536 -> '1.5KB'."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit in ("B", "KB") else f"{n:.1f}{unit}"
        n /= 1024
    raise AssertionError("unreachable: the loop returns at TB")  # pragma: no cover


@dataclass
class Job:
    """One job from .gatewright/jobs/*.yml. See docs/configuration.md for every field."""
    id: str
    gate: str
    run: str
    name: str = ""
    category: str = "dev"            # dev | sec | ops
    needs: list[str] = field(default_factory=list)
    parser: dict = field(default_factory=lambda: {"type": "generic"})
    cpu: float = 1.0
    mem: int = 256 * 1024**2
    timeout: int = 600
    retries: int = 0
    optional: bool = False           # missing tool -> skipped instead of error
    fail_on: str = "low"             # minimum unsuppressed severity that fails the job
    image: str | None = None         # run inside this container image
    network: bool = True
    container_user: str = "host"     # host: run as you (Linux) | image: the image's own user
    tool: str | None = None
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cache: bool = True
    shell: str = "auto"              # auto | sh | bash | cmd | powershell
    when_exists: list[str] = field(default_factory=list)  # run only if one of these files/globs exists
    source: str = ""                 # file the job was loaded from

    @property
    def title(self) -> str:
        return self.name or self.id

    @property
    def tool_name(self) -> str:
        """The program this job runs: `tool:` if set, else the first word of `run:`
        (for `python -m X` it is X). Used for availability checks and versions.
        """
        if self.tool:
            return self.tool
        text = self.run.strip()
        if text[:1] in ("'", '"') and text.find(text[0], 1) > 0:
            # quoted program path, e.g. "C:\Program Files\x.exe" or "/opt/my tools/x"
            # (quotes are stripped by hand: shlex would eat Windows backslashes)
            first = text[1:text.find(text[0], 1)]
        else:
            first = text.split()[0] if text else ""
        if first in ("python", "python3") and " -m " in f" {self.run} ":
            parts = self.run.split()
            idx = parts.index("-m")
            return parts[idx + 1] if idx + 1 < len(parts) else first
        return first

    def definition_hash(self) -> str:
        """Hash of every field that affects the result (part of the cache key)."""
        d = asdict(self)
        d.pop("source", None)
        return hashlib.sha256(repr(sorted(d.items())).encode()).hexdigest()


@dataclass
class CoverageRule:
    """Minimum test coverage (`coverage:` in pipeline.yml, on a gate or for the whole run).

    `missing`: what happens when no job measured coverage.
      fail   - the gate/run fails (default: a coverage requirement must never pass silently)
      ignore - the rule is reported as skipped
    """
    min: float
    missing: str = "fail"


@dataclass
class Gate:
    """A stage of the pipeline. `enforced` stops later gates on failure; `advisory` doesn't."""
    id: str
    mode: str = "enforced"           # enforced | advisory
    enabled: bool = True
    description: str = ""
    requires: list[str] = field(default_factory=list)  # gates that must have passed (this or earlier run)
    coverage: CoverageRule | None = None               # minimum coverage for this gate


@dataclass
class PolicyRule:
    """`metric op value`, e.g. findings.critical == 0. Evaluated per gate or per run."""
    id: str
    metric: str
    op: str
    value: float
    description: str = ""
    gates: list[str] = field(default_factory=list)  # empty = whole run
    severity: str = "high"
    missing: str = "skip"   # metric not produced: skip (report only) | fail


@dataclass
class Pipeline:
    """The whole configuration of one project (.gatewright/)."""
    name: str
    root: str
    gates: list[Gate]
    jobs: list[Job]
    profile: str = "auto"
    limits: dict = field(default_factory=dict)
    shell: str = "auto"
    policy: list[PolicyRule] = field(default_factory=list)
    notify: dict = field(default_factory=dict)
    vcs: dict = field(default_factory=dict)            # git host integration (watch.py)
    signing: dict = field(default_factory=dict)        # artifact signing (signing.py)
    environments: dict = field(default_factory=dict)   # deploy targets (deploy.py)
    coverage: CoverageRule | None = None               # minimum coverage for the whole run
    keep_runs: int = 0                                 # delete older runs after each run (0: keep all)

    def gate(self, gid: str) -> Gate | None:
        return next((g for g in self.gates if g.id == gid), None)

    def jobs_in(self, gid: str) -> list[Job]:
        return [j for j in self.jobs if j.gate == gid]

    def job(self, jid: str) -> Job | None:
        return next((j for j in self.jobs if j.id == jid), None)


@dataclass
class JobError:
    """One problem found by a job. Covers code findings and infra errors."""
    job_id: str
    category: str
    severity: str
    human: str                       # plain-language description
    hint: str = ""                   # suggested fix
    raw: str = ""                    # original tool output (trimmed)
    file: str | None = None
    line: int | None = None
    rule: str | None = None
    tool: str | None = None
    snippet: list[tuple[int, str]] = field(default_factory=list)  # (lineno, text)
    fingerprint: str = ""
    suppressed: bool = False
    suppression_reason: str = ""

    @property
    def is_infra(self) -> bool:
        return self.category in INFRA_CATEGORIES

    @property
    def location(self) -> str:
        if self.file and self.line:
            return f"{self.file}:{self.line}"
        return self.file or ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["snippet"] = [list(x) for x in self.snippet]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "JobError":
        d = dict(d)
        d["snippet"] = [tuple(x) for x in d.get("snippet", [])]
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class JobResult:
    """Outcome of one job in one run."""
    job_id: str
    gate: str
    category: str
    status: str
    exit_code: int | None = None
    duration: float = 0.0
    started: str = ""
    attempts: int = 0
    errors: list[JobError] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    log_file: str = ""
    outputs: list[str] = field(default_factory=list)
    peak_mem: int = 0
    peak_cpu: float = 0.0
    note: str = ""
    cache_key: str = ""
    tool_version: str = ""

    @property
    def ok(self) -> bool:
        return self.status in OK_STATUSES

    @property
    def active_errors(self) -> list[JobError]:
        return [e for e in self.errors if not e.suppressed]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["errors"] = [e.to_dict() for e in self.errors]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "JobResult":
        d = dict(d)
        d["errors"] = [JobError.from_dict(e) for e in d.get("errors", [])]
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class GateResult:
    """Outcome of one gate: its jobs, policy results and status."""
    gate: str
    mode: str
    status: str = PASSED             # passed | failed | warn | blocked | skipped
    jobs: list[JobResult] = field(default_factory=list)
    policy: list[dict] = field(default_factory=list)  # evaluated policy rules
    duration: float = 0.0
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (PASSED, WARN, SKIPPED)

    def to_dict(self) -> dict:
        return {
            "gate": self.gate, "mode": self.mode, "status": self.status,
            "duration": self.duration, "note": self.note, "policy": self.policy,
            "jobs": [j.to_dict() for j in self.jobs],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GateResult":
        return cls(
            gate=d["gate"], mode=d["mode"], status=d["status"], duration=d.get("duration", 0),
            note=d.get("note", ""), policy=d.get("policy", []),
            jobs=[JobResult.from_dict(j) for j in d.get("jobs", [])],
        )


@dataclass
class RunResult:
    """Outcome of a whole run. This is what manifest.json and the history store."""
    run_id: str
    pipeline: str
    root: str
    started: str
    finished: str = ""
    status: str = PASSED
    commit: str = ""
    branch: str = ""
    dirty: bool = False
    profile: str = ""
    hardware: dict = field(default_factory=dict)
    gates: list[GateResult] = field(default_factory=list)
    selection: dict = field(default_factory=dict)
    artifact_dir: str = ""
    resumed_from: str = ""
    duration: float = 0.0
    policy: list[dict] = field(default_factory=list)   # run-level policy results
    warnings: list[str] = field(default_factory=list)
    tool_versions: dict[str, str] = field(default_factory=dict)

    def all_jobs(self) -> list[JobResult]:
        return [j for g in self.gates for j in g.jobs]

    def all_errors(self) -> list[JobError]:
        return [e for j in self.all_jobs() for e in j.errors]

    def severity_counts(self, include_suppressed: bool = False) -> dict[str, int]:
        counts = {s: 0 for s in SEVERITIES}
        for e in self.all_errors():
            if e.suppressed and not include_suppressed:
                continue
            counts[e.severity] = counts.get(e.severity, 0) + 1
        return counts

    def to_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k != "gates"}
        d["gates"] = [g.to_dict() for g in self.gates]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RunResult":
        d = dict(d)
        gates = [GateResult.from_dict(g) for g in d.pop("gates", [])]
        r = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        r.gates = gates
        return r
