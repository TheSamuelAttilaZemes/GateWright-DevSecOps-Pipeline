# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Loading and validating pipeline configuration.

Layout inside a project:
    .gatewright/pipeline.yml     gates, profile, limits, policy
    .gatewright/jobs/*.yml       job definitions (one job, or a `jobs:` list per file)
    .gatewright/baseline.yml     accepted findings (optional)
    .gatewright/secrets.env      local secrets, never committed (optional)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from .schema_check import iter_errors

from .models import CoverageRule, Gate, Job, Pipeline, PolicyRule, parse_mem, plain

GATEWRIGHT_DIR = ".gatewright"

MEM_PATTERN = r"^\s*\d+(\.\d+)?\s*(K|KB|M|MB|G|GB)?\s*$"

JOB_SCHEMA = {
    "type": "object",
    "required": ["id", "gate", "run"],
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string", "pattern": r"^[A-Za-z0-9_.-]+$"},
        "name": {"type": "string"},
        "gate": {"type": "string"},
        "category": {"enum": ["dev", "sec", "ops"]},
        "run": {"type": "string", "minLength": 1},
        "needs": {"type": "array", "items": {"type": "string"}},
        "parser": {
            "oneOf": [
                {"type": "string"},
                {"type": "object", "required": ["type"],
                 "properties": {"type": {"type": "string"}, "file": {"type": "string"}},
                 "additionalProperties": False},
            ]
        },
        "resources": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "cpu": {"type": "number", "exclusiveMinimum": 0},
                "mem": {"oneOf": [{"type": "number"}, {"type": "string", "pattern": MEM_PATTERN}]},
            },
        },
        "timeout": {"type": "integer", "minimum": 1},
        "retries": {"type": "integer", "minimum": 0, "maximum": 10},
        "optional": {"type": "boolean"},
        "fail_on": {"enum": ["critical", "high", "medium", "low", "info"]},
        "image": {"type": ["string", "null"]},
        "network": {"type": "boolean"},
        "container_user": {"enum": ["host", "image"]},
        "tool": {"type": "string"},
        "inputs": {"type": "array", "items": {"type": "string"}},
        "outputs": {"type": "array", "items": {"type": "string"}},
        "secrets": {"type": "array", "items": {"type": "string"}},
        "env": {"type": "object", "additionalProperties": {"type": ["string", "number", "boolean"]}},
        "cache": {"type": "boolean"},
        "shell": {"enum": ["auto", "sh", "bash", "cmd", "powershell"]},
        "when_exists": {"type": "array", "items": {"type": "string"}},
    },
}

_STR_LIST = {"type": "array", "items": {"type": "string"}}
NOTIFY_EVENTS = ["run_passed", "run_warn", "run_failed", "run_cancelled", "run_skipped", "gate_failed", "approval_needed",
                 "deployed", "deploy_failed", "rolled_back"]

VCS_SCHEMA = {
    "type": "object", "required": ["provider", "repo"], "additionalProperties": False,
    "properties": {
        "provider": {"enum": ["github", "gitea", "forgejo", "gitlab"]},
        "repo": {"type": "string", "pattern": r"^[^/\s]+(/[^/\s]+)+$"},
        "api_url": {"type": "string"},
        "clone_url": {"type": "string"},
        "token_secret": {"type": "string"},
        "branches": _STR_LIST,
        "pull_requests": {"type": "boolean"},
        "mode": {"enum": ["manual", "auto-trusted", "auto"]},
        "trusted": _STR_LIST,
        "gates": _STR_LIST,
        "poll_interval": {"type": "integer", "minimum": 10},
        "untrusted": {"enum": ["run", "containers", "refuse"]},
        "parallel": {"type": "integer", "minimum": 1, "maximum": 16},
        "fetch_depth": {"type": "integer", "minimum": 0},
        "submodules": {"type": "boolean"},
        "lfs": {"type": "boolean"},
        "webhook": {
            "type": "object", "additionalProperties": False,
            "properties": {"host": {"type": "string"}, "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                           "path": {"type": "string"}, "secret": {"type": "string"}},
        },
        "report_url": {"type": "string"},
        "comment": {"type": "boolean"},
        "status_prefix": {"type": "string", "pattern": r"^[A-Za-z0-9_./-]+$"},
    },
}

SIGNING_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "key": {"type": "string"},
        "identity": {"type": "string"},
        "allowed_signers": {"type": "string"},
        "auto": {"type": "boolean"},
        "required": {"type": "boolean"},
    },
}

ENVIRONMENT_SCHEMA = {
    "type": "object", "required": ["deploy"], "additionalProperties": False,
    "properties": {
        "deploy": {"type": "string", "minLength": 1},
        "verify": {"type": "string"},
        "requires": _STR_LIST,
        "after": _STR_LIST,
        "require_signature": {"type": "boolean"},
        "auto_rollback": {"type": "boolean"},
        "approval": {"type": "boolean"},
        "timeout": {"type": "integer", "minimum": 1},
        "secrets": _STR_LIST,
        "env": {"type": "object", "additionalProperties": {"type": ["string", "number", "boolean"]}},
        "release_notes": {"type": "boolean"},
        "shell": {"enum": ["auto", "sh", "bash", "cmd", "powershell"]},
    },
}

CHANNEL_SCHEMA = {
    "type": "object", "required": ["type"], "additionalProperties": False,
    "properties": {
        "type": {"enum": ["ntfy", "webhook", "email", "matrix"]},
        "on": {"type": "array", "items": {"enum": NOTIFY_EVENTS}},
        "url": {"type": "string"},
        "token_secret": {"type": "string"},
        "secret": {"type": "string"},
        "smtp": {"type": "string"},
        "from": {"type": "string"},
        "to": _STR_LIST,
        "username_secret": {"type": "string"},
        "password_secret": {"type": "string"},
        "starttls": {"type": "boolean"},
        "homeserver": {"type": "string"},
        "room": {"type": "string"},
    },
}

NOTIFY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "on": {"type": "array", "items": {"enum": NOTIFY_EVENTS}},
        "channels": {"type": "array", "items": CHANNEL_SCHEMA},
    },
}

# `coverage: 80` or `coverage: {min: 80, missing: fail|ignore}` - on a gate, or top level for the run.
# Checked by _check_coverage() (a schema `oneOf` would only say "not valid under any of the given schemas").
COVERAGE_SCHEMA: dict = {}
COVERAGE_HELP = "use a number from 0 to 100 (e.g. `coverage: 80`) or `{min: 80, missing: fail}` (missing: fail | ignore)"


def _coverage_problem(v) -> str:
    """Why a `coverage:` value is invalid, or '' if it is fine."""
    def pct_problem(x, what):
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            return f"{what} must be a number, not {x!r}"
        if not 0 <= x <= 100:
            return f"{what} must be between 0 and 100, not {x:g}"
        return ""
    if isinstance(v, dict):
        extra = sorted(set(v) - {"min", "missing"})
        if extra:
            return f"unknown key(s) {', '.join(map(str, extra))}"
        if "min" not in v:
            return "missing `min:`"
        if v.get("missing", "fail") not in ("fail", "ignore"):
            return f"`missing:` must be fail or ignore, not {v['missing']!r}"
        return pct_problem(v["min"], "`min:`")
    return pct_problem(v, "the value")


def _check_coverage(pdata: dict, rel: str, ctx) -> None:
    """Report bad `coverage:` values in plain words and drop them, so loading can continue."""
    places = [(pdata, "coverage")]
    gates = pdata.get("gates")
    if isinstance(gates, list):   # anything else is already reported by the schema check
        places += [(g, f"gates[{i}].coverage") for i, g in enumerate(gates) if isinstance(g, dict)]
    for holder, where in places:
        if holder.get("coverage") is not None:   # `coverage:` left empty = not set
            why = _coverage_problem(holder["coverage"])
            if why:
                ctx.add(f"{rel}: {where}: {why}; {COVERAGE_HELP}")
                holder["coverage"] = None

PIPELINE_SCHEMA = {
    "type": "object",
    "required": ["gates"],
    "additionalProperties": False,
    "properties": {
        "version": {"type": "integer"},
        "name": {"type": "string"},
        "profile": {"enum": ["auto", "laptop", "server"]},
        "shell": {"enum": ["auto", "sh", "bash", "cmd", "powershell"]},
        "limits": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "max_cpu_percent": {"type": "number", "minimum": 10, "maximum": 100},
                "max_mem_percent": {"type": "number", "minimum": 10, "maximum": 100},
                "kill_mem_percent": {"type": "number", "minimum": 10, "maximum": 100},
                "max_parallel": {"type": "integer", "minimum": 1},
            },
        },
        "gates": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object", "required": ["id"], "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "pattern": r"^[A-Za-z0-9_.-]+$"},
                    "mode": {"enum": ["enforced", "advisory"]},
                    "enabled": {"type": "boolean"},
                    "description": {"type": "string"},
                    "requires": {"type": "array", "items": {"type": "string"}},
                    "coverage": COVERAGE_SCHEMA,
                },
            },
        },
        "coverage": COVERAGE_SCHEMA,
        "keep_runs": {"type": "integer", "minimum": 1},
        "policy": {
            "type": "array",
            "items": {
                "type": "object", "required": ["id", "metric", "op", "value"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "metric": {"type": "string"},
                    "op": {"enum": ["==", "!=", "<", "<=", ">", ">="]},
                    "value": {"type": "number"},
                    "description": {"type": "string"},
                    "gates": {"type": "array", "items": {"type": "string"}},
                    "severity": {"enum": ["critical", "high", "medium", "low", "info"]},
                },
            },
        },
        "notify": NOTIFY_SCHEMA,
        "vcs": VCS_SCHEMA,
        "signing": SIGNING_SCHEMA,
        "environments": {"type": "object", "additionalProperties": ENVIRONMENT_SCHEMA},
        "jobs": {"type": "array", "items": JOB_SCHEMA},
    },
}


class ConfigError(Exception):
    """Raised with a list of human-readable problems."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


@dataclass
class _Ctx:
    problems: list[str] = field(default_factory=list)

    def add(self, msg: str) -> None:
        self.problems.append(msg)


def _path_str(path) -> str:
    out = ""
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else str(p))
    return out or "(top level)"


def _validate(data, schema, source: str, ctx: _Ctx) -> None:
    for err in sorted(iter_errors(data, schema), key=lambda e: [str(p) for p in e.absolute_path]):
        where = _path_str(err.absolute_path)
        msg = err.message
        if err.validator == "additionalProperties":
            msg = f"unknown key ({msg.split('(')[-1].rstrip(')')})"
        elif err.validator == "enum":
            msg = f"must be one of {err.validator_value}, got {err.instance!r}"
        elif err.validator == "required":
            msg = f"missing required key: {msg.split(' ')[0]}"
        ctx.add(f"{source}: {where}: {msg}")


class YAMLLoader(yaml.SafeLoader):
    """Safe YAML loader with YAML 1.2 booleans: only true/false are booleans.

    YAML 1.1 (PyYAML's default) also turns on/off/yes/no into booleans, so a key
    written `on:` (as in `notify: on: [...]`) would silently become `True:`."""


YAMLLoader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:bool"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for _first in "tTfF":
    YAMLLoader.yaml_implicit_resolvers.setdefault(_first, []).insert(
        0, ("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")))


def load_yaml_text(text: str):
    """Parse YAML the way all Gatewright config files are parsed."""
    return yaml.load(text, Loader=YAMLLoader)  # nosec B506 - YAMLLoader is a SafeLoader subclass


def _load_yaml(path: Path, ctx: _Ctx):
    try:
        return load_yaml_text(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        ctx.add(f"{path}:{where} invalid YAML: {getattr(e, 'problem', e)}")
    except UnicodeDecodeError:
        ctx.add(f"{path}: is not UTF-8 text; save it as UTF-8")
    except OSError as e:
        ctx.add(f"{path}: cannot read file: {e}")
    return None


def _job_from_dict(d: dict, source: str, default_shell: str = "auto") -> Job:
    res = d.get("resources", {}) or {}
    parser = d.get("parser", "generic")
    if isinstance(parser, str):
        parser = {"type": parser}
    return Job(
        id=d["id"], gate=d["gate"], run=d["run"], name=plain(d.get("name", "")),
        category=d.get("category", "dev"), needs=list(d.get("needs", [])), parser=parser,
        cpu=float(res.get("cpu", 1)), mem=parse_mem(res.get("mem", "256MB")),
        timeout=int(d.get("timeout", 600)), retries=int(d.get("retries", 0)),
        optional=bool(d.get("optional", False)), fail_on=d.get("fail_on", "low"),
        image=d.get("image"), network=bool(d.get("network", True)),
        container_user=d.get("container_user", "host"), tool=d.get("tool"),
        inputs=list(d.get("inputs", [])), outputs=list(d.get("outputs", [])),
        secrets=list(d.get("secrets", [])), env={k: str(v) for k, v in (d.get("env") or {}).items()},
        cache=bool(d.get("cache", True)), shell=d.get("shell", default_shell), source=source,
        when_exists=list(d.get("when_exists", [])),  # nosec B604 - `shell` is a config string, not subprocess
    )


def coverage_rule(v) -> CoverageRule | None:
    """`coverage:` value (already schema-checked) -> CoverageRule, or None if not set."""
    if v is None:
        return None
    if isinstance(v, dict):
        return CoverageRule(float(v["min"]), v.get("missing", "fail"))
    return CoverageRule(float(v))


def find_root(start: str | os.PathLike = ".") -> Path:
    """The nearest directory (this one or a parent) that contains .gatewright/pipeline.yml."""
    p = Path(start).resolve()
    for cand in [p, *p.parents]:
        if (cand / GATEWRIGHT_DIR / "pipeline.yml").exists():
            return cand
    return p


def load_pipeline(root: str | os.PathLike = ".") -> Pipeline:
    """Load and validate a project's pipeline. Raises ConfigError with a list of
    human-readable problems (file, location, what is wrong) - all at once, not
    just the first.
    """
    root = find_root(root)
    fdir = root / GATEWRIGHT_DIR
    ctx = _Ctx()
    pfile = fdir / "pipeline.yml"
    if not pfile.exists():
        raise ConfigError([f"No pipeline found: {pfile} does not exist. Run `gatewright init` to create one."])

    pdata = _load_yaml(pfile, ctx)
    if pdata is None:
        raise ConfigError(ctx.problems)
    if not isinstance(pdata, dict):
        raise ConfigError([f"{pfile.relative_to(root)}: must be a mapping with `gates:` (and optionally `policy:`, "
                           f"`vcs:` ...), not a {type(pdata).__name__}"])
    _validate(pdata, PIPELINE_SCHEMA, str(pfile.relative_to(root)), ctx)
    _check_coverage(pdata, str(pfile.relative_to(root)), ctx)

    raw_jobs: list[tuple[dict, str]] = []
    for i, j in enumerate(pdata.get("jobs", []) or []):
        raw_jobs.append((j, f"{pfile.relative_to(root)}:jobs[{i}]"))

    jdir = fdir / "jobs"
    if jdir.is_dir():
        for jf in sorted([*jdir.glob("*.yml"), *jdir.glob("*.yaml")]):
            rel = str(jf.relative_to(root))
            data = _load_yaml(jf, ctx)
            if data is None or data == {}:   # unreadable (already reported) or empty file
                continue
            items = data.get("jobs") if isinstance(data, dict) and "jobs" in data else [data]
            if not isinstance(items, list):
                ctx.add(f"{rel}: `jobs` must be a list")
                continue
            for i, item in enumerate(items):
                src = rel if len(items) == 1 else f"{rel}:jobs[{i}]"
                if not isinstance(item, dict):
                    ctx.add(f"{src}: a job must be a mapping")
                    continue
                _validate(item, JOB_SCHEMA, src, ctx)
                raw_jobs.append((item, src))

    if ctx.problems:
        raise ConfigError(ctx.problems)

    gates = [Gate(id=g["id"], mode=g.get("mode", "enforced"), enabled=g.get("enabled", True),
                  description=plain(g.get("description", "")), requires=list(g.get("requires", [])),
                  coverage=coverage_rule(g.get("coverage")))
             for g in pdata["gates"]]
    jobs = [_job_from_dict(d, s, pdata.get("shell", "auto")) for d, s in raw_jobs]
    policy = [PolicyRule(id=p["id"], metric=p["metric"], op=p["op"], value=float(p["value"]),
                         description=p.get("description", ""), gates=list(p.get("gates", [])),
                         severity=p.get("severity", "high"))
              for p in pdata.get("policy", []) or []]

    pipe = Pipeline(name=plain(pdata.get("name", root.name)), root=str(root), gates=gates, jobs=jobs,
                    profile=pdata.get("profile", "auto"), shell=pdata.get("shell", "auto"), limits=pdata.get("limits", {}) or {},  # nosec B604 - `shell` is a config string, not subprocess
                    policy=policy, notify=pdata.get("notify", {}) or {},
                    vcs=pdata.get("vcs") or {}, signing=pdata.get("signing") or {},
                    environments=pdata.get("environments") or {}, coverage=coverage_rule(pdata.get("coverage")),
                    keep_runs=pdata.get("keep_runs") or 0)
    _semantic_checks(pipe, ctx)
    if ctx.problems:
        raise ConfigError(ctx.problems)
    return pipe


def _semantic_checks(p: Pipeline, ctx: _Ctx) -> None:
    from .graph import CycleError, toposort

    gate_ids = [g.id for g in p.gates]
    gate_order = {g: i for i, g in enumerate(gate_ids)}
    if len(set(gate_ids)) != len(gate_ids):
        ctx.add("pipeline.yml: duplicate gate ids")
    for g in p.gates:
        for r in g.requires:
            if r not in gate_order:
                ctx.add(f"pipeline.yml: gate '{g.id}' requires unknown gate '{r}'")
            elif gate_order[r] >= gate_order[g.id]:
                ctx.add(f"pipeline.yml: gate '{g.id}' can only require gates listed before it ('{r}' is not)")

    seen: dict[str, str] = {}
    for j in p.jobs:
        if j.id in seen:
            ctx.add(f"{j.source}: duplicate job id '{j.id}' (also defined in {seen[j.id]})")
        seen[j.id] = j.source
        if j.gate not in gate_order:
            ctx.add(f"{j.source}: job '{j.id}' uses unknown gate '{j.gate}'. Known gates: {', '.join(gate_ids)}")
    by_id = {j.id: j for j in p.jobs}
    for j in p.jobs:
        for n in j.needs:
            if n not in by_id:
                ctx.add(f"{j.source}: job '{j.id}' needs unknown job '{n}'")
            elif j.gate in gate_order and by_id[n].gate in gate_order and \
                    gate_order[by_id[n].gate] > gate_order[j.gate]:
                ctx.add(f"{j.source}: job '{j.id}' (gate {j.gate}) cannot need '{n}' from a later gate ({by_id[n].gate})")
    for r in p.policy:
        for g in r.gates:
            if g not in gate_order:
                ctx.add(f"pipeline.yml: policy '{r.id}' references unknown gate '{g}'")
    for g in p.gates:
        # a gate without jobs never runs, so its minimum would never be checked: say so now
        if g.coverage and g.coverage.missing == "fail" and not p.jobs_in(g.id):
            ctx.add(f"pipeline.yml: gate '{g.id}' sets `coverage: {g.coverage.min:g}` but has no jobs; add a job with "
                    "`parser: coverage` to it, or remove the minimum (or set `missing: ignore`)")
    vcs = p.vcs or {}
    for g in vcs.get("gates", []):
        if g not in gate_order:
            ctx.add(f"pipeline.yml: vcs.gates lists unknown gate '{g}'")
    if vcs.get("provider") in ("gitea", "forgejo") and not vcs.get("api_url"):
        ctx.add("pipeline.yml: vcs.api_url is required for Gitea/Forgejo, e.g. https://codeberg.org/api/v1")
    envs = p.environments or {}
    for name, env in envs.items():
        for g in env.get("requires", []):
            if g not in gate_order:
                ctx.add(f"pipeline.yml: environment '{name}' requires unknown gate '{g}'")
        for other in env.get("after", []):
            if other == name:
                ctx.add(f"pipeline.yml: environment '{name}' cannot come after itself")
            elif other not in envs:
                ctx.add(f"pipeline.yml: environment '{name}' comes after unknown environment '{other}'")
    if not ctx.problems:
        try:
            toposort(p.jobs)
        except CycleError as e:
            ctx.add(f"jobs: dependency cycle detected: {' -> '.join(e.cycle)}")
