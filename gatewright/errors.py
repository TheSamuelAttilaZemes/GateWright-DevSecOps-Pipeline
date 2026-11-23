# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Turning raw tool output into human-readable, structured errors.

Two halves:
  * classify_infra() - "the pipeline is broken": tool missing, timeout, out of
    memory, Docker down, network... (INFRA, shown orange)
  * parse_output()   - "your code is broken": lint findings, security findings,
    failed tests, vulnerable dependencies... (CODE, shown red)

Each parser returns JobError objects with a plain-language `human` text, a
`hint`, and file/line, so the UI and reports can show the code in red.

How to add support for a new tool:
  1. write `@parser("mytool") def parse_mytool(job, stdout, stderr, code, root, report)`
     returning `(list[JobError], metrics_dict)`; raise ParseError if the output
     is unreadable (Gatewright then reports a crash instead of "no findings")
  2. optionally add friendlier texts for its rules to CATALOG
  3. use it in a job with `parser: mytool` (or `parser: {type: mytool, file: ...}`)
  4. add a test with a captured sample of the tool's real output (tests/test_parsers.py)
If the tool can emit SARIF, `parser: sarif` already works without any code.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET  # nosec B405 - parsing local tool reports only
from pathlib import Path
from typing import Callable

from .models import Job, JobError, plain
from .winsupport import true_case

MAX_RAW = 4000

# ---------------------------------------------------------------------------
# Catalog of known rules -> (human description, hint)
# ---------------------------------------------------------------------------
CATALOG: dict[tuple[str, str], tuple[str, str]] = {
    # bandit
    ("bandit", "B101"): ("`assert` is used for a runtime check in {loc}. Python removes asserts when run "
                         "with -O, so this check silently disappears in optimized builds.",
                         "Replace the assert with an explicit `if ...: raise ...`."),
    ("bandit", "B105"): ("A password or secret appears to be hardcoded in {loc}. Anyone with access to the "
                         "code (or its git history) can read it.",
                         "Move the value to an environment variable or a secret store, then rotate the secret."),
    ("bandit", "B106"): ("A hardcoded password is passed as a function argument in {loc}.",
                         "Read the password from the environment or a secret store instead."),
    ("bandit", "B107"): ("A function has a hardcoded password as a default argument in {loc}.",
                         "Remove the default and require the caller to supply it from configuration."),
    ("bandit", "B108"): ("A hardcoded temporary file/directory path is used in {loc}; other users on the machine "
                         "can predict and hijack it.", "Use the `tempfile` module (e.g. tempfile.mkstemp())."),
    ("bandit", "B301"): ("`pickle` is used to load data in {loc}. Unpickling untrusted data can run arbitrary code.",
                         "Use JSON or another safe format for data that crosses a trust boundary."),
    ("bandit", "B303"): ("A weak hash (MD5/SHA1) is used in {loc}.",
                         "Use hashlib.sha256 or better; for passwords use bcrypt/argon2."),
    ("bandit", "B324"): ("A weak hash (MD5/SHA1) is used in {loc}.",
                         "Use hashlib.sha256, or pass usedforsecurity=False if it is not security related."),
    ("bandit", "B307"): ("`eval()` is used in {loc}. If any part of the input comes from a user, they can run any code.",
                         "Use ast.literal_eval() for literals, or parse the input explicitly."),
    ("bandit", "B311"): ("The `random` module is used in {loc}. It is predictable and not safe for tokens or passwords.",
                         "Use the `secrets` module for anything security related."),
    ("bandit", "B501"): ("An HTTPS request disables certificate verification (verify=False) in {loc}. "
                         "This allows man-in-the-middle attacks.", "Remove verify=False; point to a CA bundle if needed."),
    ("bandit", "B506"): ("yaml.load() is used without a safe loader in {loc}; crafted YAML can run code.",
                         "Use yaml.safe_load()."),
    ("bandit", "B602"): ("A subprocess is started with shell=True in {loc}. If any argument contains user input, "
                         "it can be used to inject shell commands.",
                         "Pass the command as a list and drop shell=True."),
    ("bandit", "B605"): ("A shell command is started with os.system() or similar in {loc}; it is open to injection.",
                         "Use subprocess.run([...]) with a list of arguments."),
    ("bandit", "B608"): ("An SQL query is built with string formatting in {loc}. This is the classic SQL injection bug.",
                         "Use parameterized queries (cursor.execute(sql, params))."),
    # ruff / pyflakes
    ("ruff", "F401"): ("{loc} imports something that is never used.", "Delete the import (ruff check --fix can do it)."),
    ("ruff", "F841"): ("{loc} assigns a variable that is never used.", "Remove it, or rename it to `_` if intentional."),
    ("ruff", "F821"): ("{loc} uses a name that is not defined. This will crash with NameError when the line runs.",
                       "Check for a typo or a missing import."),
    ("ruff", "F811"): ("{loc} redefines a name that was already defined and never used.", "Remove the duplicate."),
    ("ruff", "E711"): ("{loc} compares to None with == / !=.", "Use `is None` / `is not None`."),
    ("ruff", "E722"): ("{loc} uses a bare `except:` that also swallows Ctrl+C and system exits.",
                       "Catch a specific exception, or at least `except Exception:`."),
    ("ruff", "E501"): ("{loc} is longer than the configured line length.", "Wrap the line or run the formatter."),
    ("ruff", "E999"): ("{loc} has a syntax error; Python cannot even load this file.", "Fix the syntax at the marked line."),
}

INFRA_PATTERNS: list[tuple[re.Pattern, str, str, str]] = [
    (re.compile(r"command not found|is not recognized as an internal or external command|No such file or directory: '[^']+'$", re.M),
     "tool_missing", "The command `{tool}` could not be found on this machine.",
     "Install it (run `gatewright doctor` for install instructions) or mark the job `optional: true`."),
    (re.compile(r"No module named ['\"]?([\w.]+)"),
     "tool_missing", "Python could not import the module `{m1}` needed by this job.",
     "Install it into the Python your jobs use (`gatewright doctor` shows which): `python -m pip install {m1}`, "
     "or put your project in a venv (.venv) that has it."),
    (re.compile(r"Unable to find image '([^']+)'|pull access denied for ([^,\s]+)|manifest unknown|failed to resolve reference \"?([^\"\s:]+)", re.I),
     "infra", "The container image `{m1}` is not available on this machine and could not be downloaded.",
     "Pull or build it first (`docker pull {m1}` / `docker build -t {m1} .`), or check the image name and your registry access."),
    (re.compile(r"Cannot connect to the Docker daemon|failed to connect to the docker API|Is the docker daemon running|"
                r"docker_engine.*(cannot find|The system cannot)|error during connect", re.I),
     "infra", "Docker is not running or not reachable, so the container for this job could not start.",
     "Start Docker (e.g. `sudo systemctl start docker`) or remove `image:` to run the job directly."),
    (re.compile(r"Permission denied", re.I),
     "infra", "The job was denied permission to read, write or execute a file.",
     "Check file permissions and that the job is not writing outside the project."),
    (re.compile(r"No space left on device", re.I),
     "resource", "The disk is full; the job could not write its output.",
     "Free disk space, or clean old runs with `rm -rf .gatewright/runs/<old-id>`."),
    (re.compile(r"(Temporary failure in name resolution|Could not resolve host|Connection (refused|reset|timed out)|Network is unreachable)", re.I),
     "infra", "The job could not reach the network.",
     "Check your connection/proxy. Network errors are retried if the job sets `retries:`."),
    (re.compile(r"MemoryError|Killed\s*$|out of memory", re.I | re.M),
     "resource", "The job ran out of memory.", "Raise `resources.mem` for the job, or run it on a bigger machine."),
]


def _shorten(text: str, limit: int) -> str:
    """Cut at a sentence or word boundary, never mid-word."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    dot = cut.rfind(". ")
    if dot > limit * 0.5:
        return cut[:dot + 1]
    return cut.rsplit(" ", 1)[0] + "…"


def _loc(file: str | None, line: int | None) -> str:
    if file and line:
        return f"`{file}` line {line}"
    if file:
        return f"`{file}`"
    return "the code"


def relpath(path: str | None, root: str) -> str | None:
    """Make a tool-reported path relative to the project, with forward slashes.

    Handles absolute paths, file:// URIs (also file:///C:/...) and paths on
    another Windows drive. The result is identical on every OS so fingerprints,
    baselines and diffs match across machines.
    """
    if not path:
        return path
    p = path
    if p.startswith("file:///"):
        p = p[8:] if re.match(r"file:///[A-Za-z]:", p) else p[7:]
    elif p.startswith("file://"):
        p = p[7:]
    try:
        rp = os.path.relpath(os.path.abspath(os.path.join(root, p)), root)
        out = p if rp.startswith("..") else rp
        inside = not rp.startswith("..")
    except ValueError:  # different drive on Windows
        out, inside = p, False
    # one canonical form on every OS, so fingerprints/baselines match across machines
    out = out.replace("\\", "/")
    # Windows paths ignore case, fingerprints don't: use the spelling on disk
    return true_case(root, out) if inside and sys.platform == "win32" else out


def snippet(root: str, file: str | None, line: int | None, context: int = 3) -> list[tuple[int, str]]:
    """Up to `context` lines before and after `line`, as (number, text) pairs."""
    if not file or not line:
        return []
    path = Path(root) / file
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    lo, hi = max(1, line - context), min(len(lines), line + context)
    return [(n, lines[n - 1]) for n in range(lo, hi + 1)]


OWN_FILES = re.compile(r"\.gatewright/(runs|cache|deployments|workspaces)/")
HIDDEN = "(not shown: this line contains a secret)"


def finalize(err: JobError, root: str) -> JobError:
    """Fill snippet + fingerprint. Fingerprint uses the line text, not the line
    number, so it survives code moving up or down."""
    if any(text == HIDDEN for _, text in err.snippet):
        return err               # finalized before; the line text is gone, so the fingerprint must stay
    err.file = relpath(err.file, root)
    err.human, err.hint, err.raw = plain(err.human), plain(err.hint), plain(err.raw)
    if err.file and err.line and not err.snippet:
        err.snippet = snippet(root, err.file, err.line)
    err.snippet = [(n, plain(t)) for n, t in err.snippet]
    line_text = next((t for n, t in err.snippet if n == err.line), "")
    basis = "|".join([err.tool or "", err.rule or err.category, err.file or "",
                      line_text.strip() or err.human[:120]])
    err.fingerprint = hashlib.sha256(basis.encode()).hexdigest()[:16]
    if err.category == "secret":     # reports and artifacts are shared: never copy the secret into them
        err.snippet = [(err.line or 0, HIDDEN)]
    if len(err.raw) > MAX_RAW:
        err.raw = err.raw[:MAX_RAW] + "\n... (truncated)"
    return err


def finalize_all(errs: list[JobError], root: str) -> list[JobError]:
    """finalize() every error, and tell identical lines apart: the first one in a file keeps the
    plain fingerprint, the second, third, ... get their own (so one can't hide behind another)."""
    for e in errs:
        finalize(e, root)
    by_print: dict[str, list[JobError]] = {}
    for e in errs:
        by_print.setdefault(e.fingerprint, []).append(e)
    for fp, same in by_print.items():
        lines = sorted({e.line or 0 for e in same})
        for e in same:
            n = lines.index(e.line or 0)
            if n:
                e.fingerprint = hashlib.sha256(f"{fp}#{n + 1}".encode()).hexdigest()[:16]
    return errs


def describe(tool: str, rule: str, message: str, file: str | None, line: int | None,
             default_hint: str = "") -> tuple[str, str]:
    """Human text + hint for a tool rule: from CATALOG if known, else the tool's own message."""
    loc = _loc(file, line)
    if (tool, rule) in CATALOG:
        human, hint = CATALOG[(tool, rule)]
        return human.format(loc=loc, message=message), hint
    return f"{message.rstrip('.')} ({loc}).", default_hint


# ---------------------------------------------------------------------------
# Infra classification
# ---------------------------------------------------------------------------
def classify_infra(job: Job, exit_code: int | None, output: str, timed_out: bool,
                   killed_reason: str = "", patterns: bool = True) -> JobError | None:
    """Decide whether a failed job failed because of the *environment*.

    Returns an INFRA JobError (tool_missing, timeout, resource, infra, crash)
    or None when the failure should be treated as a code problem. Checked
    before parsing, so e.g. 'pytest not installed' is never reported as
    'tests failed'.
    """
    tool = job.tool_name
    if killed_reason:
        return JobError(job.id, "resource", "high",
                        f"Gatewright stopped this job to protect the machine: {killed_reason}.",
                        "Lower parallelism (limits.max_parallel), raise the job's `resources.mem`, "
                        "or run on a machine with more memory.", raw=output[-MAX_RAW:], tool=tool)
    if timed_out:
        return JobError(job.id, "timeout", "high",
                        f"The job did not finish within its time limit of {job.timeout} seconds and was stopped.",
                        "Raise `timeout:` for the job, or look in the log for where it hangs "
                        "(waiting for input or network is a common cause).", raw=output[-MAX_RAW:], tool=tool)
    if job.image and exit_code in (126, 127):
        # newer docker CLIs also use 127 when the daemon is down ("...: no such file or directory")
        for pat, cat, human, hint in INFRA_PATTERNS:
            if cat == "infra" and "Docker is not running" in human and pat.search(output[-6000:]):
                return JobError(job.id, cat, "high", human, hint, raw=output[-MAX_RAW:], tool=tool)
    if exit_code == 127 and job.image:
        return JobError(job.id, "tool_missing", "high",
                        f"The command `{tool}` could not be found in the container image `{job.image}`.",
                        "Use an image that contains it, or check the spelling in `run:`.",
                        raw=output[-MAX_RAW:], tool=tool)
    if exit_code == 127:
        return JobError(job.id, "tool_missing", "high",
                        f"The command `{tool}` could not be found on this machine.",
                        "Install it (run `gatewright doctor` for install instructions) or mark the job `optional: true`.",
                        raw=output[-MAX_RAW:], tool=tool)
    if exit_code in (137, -9) and not timed_out:
        return JobError(job.id, "resource", "high",
                        "The job was killed by the operating system (usually because it ran out of memory).",
                        "Raise `resources.mem`, or reduce what runs in parallel.", raw=output[-MAX_RAW:], tool=tool)
    if exit_code == 126:
        return JobError(job.id, "infra", "high", f"`{tool}` was found but is not executable.",
                        f"Run `chmod +x` on it, or check that `{tool}` is the right file.",
                        raw=output[-MAX_RAW:], tool=tool)
    if patterns and exit_code not in (0, None):
        tail = output[-6000:]
        for pat, cat, human, hint in INFRA_PATTERNS:
            m = pat.search(tail)
            if m:
                m1 = next((g for g in m.groups() if g), "") or (job.image or "")
                if cat == "tool_missing" and m1 and _module_is_project_code(m1, job):
                    continue  # a missing project module is a code bug, not infra
                return JobError(job.id, cat, "high", human.format(tool=tool, m1=m1),
                                hint.format(tool=tool, m1=m1), raw=tail[-MAX_RAW:], tool=tool)
    return None


def _module_is_project_code(module: str, job: Job) -> bool:
    # If the job runs pytest, an import error is almost always the user's code.
    return job.tool_name in ("pytest",) and module not in ("pytest",)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------
ParserFn = Callable[[Job, str, str, int, str, str | None], tuple[list[JobError], dict]]
PARSERS: dict[str, ParserFn] = {}


def parser(name: str):
    """Decorator that registers a parser function under a name usable in `parser:`."""
    def deco(fn):
        PARSERS[name] = fn
        return fn
    return deco


class ParseError(Exception):
    """Raise from a parser when the tool output cannot be read at all."""
    pass


class NoOutput(ParseError):
    """The tool printed nothing."""


def _json(text: str):
    text = text.strip()
    if not text:
        raise NoOutput("the tool produced no output")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # some tools print a banner before the JSON
        for opener in ("{", "["):
            i = text.find(opener)
            if i > 0:
                try:
                    return json.loads(text[i:])
                except json.JSONDecodeError:
                    pass
        raise ParseError("the tool's output was not valid JSON") from None


_LOC_PATTERNS = [
    re.compile(r'File "([^"]+)", line (\d+)'),
    re.compile(r"^((?:[A-Za-z]:)?[\w./\\ -]+?\.\w+):(\d+)(?::\d+)?[: ]", re.M),
]


def _first_location(text: str, root: str) -> tuple[str | None, int | None]:
    best = (None, None)
    for pat in _LOC_PATTERNS:
        for m in pat.finditer(text):
            f = relpath(m.group(1), root)
            if f and not f.startswith("..") and (Path(root) / f).exists() and "site-packages" not in f:
                best = (f, int(m.group(2)))  # keep the last project frame (closest to the error)
        if best[0]:
            return best
    return best


@parser("generic")
def parse_generic(job, stdout, stderr, code, root, report):
    """Fallback for any tool: exit code != 0 means failure; tries to find a
    file:line in the output (Python tracebacks, compiler style) for the snippet.
    """
    if code == 0:
        return [], {}
    out = (stdout + "\n" + stderr).strip()
    file, line = _first_location(out, root)
    last = next((ln.strip() for ln in reversed(out.splitlines()) if ln.strip()), "")
    human = f"`{job.title}` failed with exit code {code}."
    if last:
        human += f" Last message: {last[:300]}"
    return [JobError(job.id, "job_failure", "high", human,
                     "Open the job log for the full output.", raw=out[-MAX_RAW:],
                     file=file, line=line, tool=job.tool_name)], {}


@parser("ruff")
def parse_ruff(job, stdout, stderr, code, root, report):
    """`ruff check --output-format json`."""
    data = _json(report or stdout) if (report or stdout.strip()) else []
    errs = []
    for item in data:
        rule = item.get("code") or "syntax"
        file = item.get("filename")
        line = (item.get("location") or {}).get("row")
        msg = item.get("message", "")
        human, hint = describe("ruff", rule, msg, relpath(file, root), line,
                               "Run `ruff check --fix` or see https://docs.astral.sh/ruff/rules/")
        sev = "high" if rule.startswith(("E9", "S")) or rule == "syntax" else \
              "medium" if rule.startswith("F") else "low"
        errs.append(JobError(job.id, "lint", sev, human, hint, raw=f"{rule} {msg}",
                             file=file, line=line, rule=rule, tool="ruff"))
    return errs, {"lint.issues": len(errs)}


@parser("bandit")
def parse_bandit(job, stdout, stderr, code, root, report):
    """`bandit -f json`."""
    data = _json(report or stdout)
    errs = []
    for r in data.get("results", []):
        rule = r.get("test_id", "")
        file, line = r.get("filename"), r.get("line_number")
        human, hint = describe("bandit", rule, r.get("issue_text", ""), relpath(file, root), line,
                               r.get("more_info", ""))
        sev = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}.get(r.get("issue_severity", "LOW"), "low")
        errs.append(JobError(job.id, "finding", sev, human, hint,
                             raw=f"[{rule}:{r.get('test_name', '')}] {r.get('issue_text', '')}",
                             file=file, line=line, rule=rule, tool="bandit"))
    return errs, {}


@parser("junit")
def parse_junit(job, stdout, stderr, code, root, report):
    """JUnit XML (pytest --junitxml, and most test runners in any language).
    Also reports tests.total / tests.failed / tests.skipped / tests.passed metrics.
    """
    if not report:
        raise ParseError("JUnit parser needs `parser.file` pointing to the XML report")
    try:
        tree = ET.fromstring(report)  # nosec B314 - local report produced by our own job
    except ET.ParseError as e:
        raise ParseError(f"the JUnit XML report is not valid XML ({e})") from None
    errs, total, failed, skipped = [], 0, 0, 0
    for tc in tree.iter("testcase"):
        total += 1
        name = tc.get("name", "?")
        cls = tc.get("classname", "")
        if tc.find("skipped") is not None:
            skipped += 1
        for tag in ("failure", "error"):
            node = tc.find(tag)
            if node is None:
                continue
            failed += 1
            text = (node.text or "") + "\n" + (node.get("message") or "")
            file, line = _first_location(text, root)
            if not file and tc.get("file"):
                raw_line = tc.get("line") or ""
                file, line = tc.get("file"), (int(raw_line) if raw_line.isdigit() and int(raw_line) > 0 else None)
            msg = (node.get("message") or "").strip().splitlines()[0:1]
            reason = msg[0][:300] if msg else "see the log for details"
            if tag == "failure":
                human = f"Test `{name}` ({cls}) failed: {reason}"
                hint = "The code no longer behaves as the test expects. Fix the code, or update the test if the behaviour change is intended."
            else:
                human = f"Test `{name}` ({cls}) crashed before it could finish: {reason}"
                hint = "An exception was raised in the test or its setup; check the marked line."
            errs.append(JobError(job.id, "test_failure", "high", human, hint, raw=text.strip(),
                                 file=file, line=line, rule=f"{cls}::{name}", tool="pytest"))
    metrics = {"tests.total": total, "tests.failed": failed, "tests.skipped": skipped,
               "tests.passed": total - failed - skipped}
    return errs, metrics


PARSERS["pytest"] = parse_junit

_SARIF_SEV = {"error": "high", "warning": "medium", "note": "low", "none": "info"}


@parser("sarif")
def parse_sarif(job, stdout, stderr, code, root, report):
    """SARIF 2.1 - the standard format of semgrep, trivy, hadolint, CodeQL and many others.
    Uses `security-severity` when the tool provides it.
    """
    data = _json(report or stdout)
    errs = []
    for run in data.get("runs", []):
        tool = ((run.get("tool") or {}).get("driver") or {}).get("name", job.tool_name).lower()
        rules = {r.get("id"): r for r in ((run.get("tool") or {}).get("driver") or {}).get("rules", [])}
        for res in run.get("results", []):
            rid = res.get("ruleId", "")
            msg = (res.get("message") or {}).get("text", "")
            loc = ((res.get("locations") or [{}])[0].get("physicalLocation") or {})
            file = (loc.get("artifactLocation") or {}).get("uri")
            line = (loc.get("region") or {}).get("startLine")
            sev = _SARIF_SEV.get(res.get("level", "warning"), "medium")
            props = (rules.get(rid) or {}).get("properties", {})
            if "security-severity" in props:
                try:
                    s = float(props["security-severity"])
                    sev = "critical" if s >= 9 else "high" if s >= 7 else "medium" if s >= 4 else "low"
                except (TypeError, ValueError):   # null / "n/a": keep the level-based severity
                    pass
            help_text = ((rules.get(rid) or {}).get("help") or {}).get("text", "")
            human, hint = describe(tool, rid, msg, relpath(file, root), line, help_text[:300])
            errs.append(JobError(job.id, "finding", sev, human, hint, raw=f"{rid}: {msg}",
                                 file=file, line=line, rule=rid, tool=tool))
    return errs, {}


@parser("gitleaks")
def parse_gitleaks(job, stdout, stderr, code, root, report):
    """`gitleaks detect --report-format json`. Every leaked secret is critical."""
    data = _json(report or stdout) if (report or stdout.strip()) else []
    errs, own = [], 0
    for f in data or []:
        file, line = f.get("File"), f.get("StartLine")
        if OWN_FILES.match(relpath(file, root) or ""):
            own += 1      # an earlier run's report about a secret: it is not in the project (any more)
            continue
        errs.append(JobError(
            job.id, "secret", "critical",
            f"A secret ({f.get('Description') or f.get('RuleID')}) was found in {_loc(relpath(file, root), line)}. "
            "If this was ever pushed, assume it is compromised.",
            "Remove it from the code, rotate the secret at its provider, and purge it from git history.",
            raw=f"{f.get('RuleID')}: {f.get('Description')}", file=file, line=line,
            rule=f.get("RuleID"), tool="gitleaks"))
    return errs, ({"findings.ignored": own} if own else {})


@parser("pip-audit")
def parse_pip_audit(job, stdout, stderr, code, root, report):
    """`pip-audit -f json`: one finding per known vulnerability."""
    data = _json(report or stdout)
    deps = data.get("dependencies", data) if isinstance(data, dict) else data
    errs = []
    for d in deps:
        for v in d.get("vulns", []):
            fix = ", ".join(v.get("fix_versions", [])) or "no fixed version yet"
            errs.append(JobError(
                job.id, "vulnerability", "high",
                f"The dependency `{d.get('name')}` {d.get('version')} has a known vulnerability ({v.get('id')}): "
                f"{_shorten((v.get('description') or '').strip(), 260)}",
                f"Upgrade `{d.get('name')}` to: {fix}.", raw=json.dumps(v)[:MAX_RAW],
                rule=v.get("id"), tool="pip-audit"))
    return errs, {"deps.vulnerable": len(errs)}


_MYPY = re.compile(r"^(?P<file>(?:[A-Za-z]:)?[^:\n]+):(?P<line>\d+):(?:\d+:)? error: (?P<msg>.*?)(?:\s+\[(?P<code>[\w-]+)\])?$", re.M)


@parser("mypy")
def parse_mypy(job, stdout, stderr, code, root, report):
    """mypy's default text output (`file:line: error: message  [code]`)."""
    errs = []
    for m in _MYPY.finditer(stdout):
        errs.append(JobError(job.id, "lint", "medium",
                             f"Type error in {_loc(relpath(m['file'], root), int(m['line']))}: {m['msg']}",
                             "Fix the types or add an annotation; see https://mypy.readthedocs.io/",
                             raw=m.group(0), file=m["file"], line=int(m["line"]), rule=m["code"] or "mypy",
                             tool="mypy"))
    return errs, {}


@parser("coverage")
def parse_coverage(job, stdout, stderr, code, root, report):
    """coverage.py JSON report -> `coverage.percent` metric for policies."""
    data = _json(report or stdout)
    pct = float((data.get("totals") or {}).get("percent_covered", 0.0))
    return [], {"coverage.percent": round(pct, 2)}


def parse_output(job: Job, stdout: str, stderr: str, exit_code: int, root: str,
                 report_text: str | None) -> tuple[list[JobError], dict, JobError | None]:
    """Returns (errors, metrics, parse_problem)."""
    ptype = job.parser.get("type", "generic")
    fn = PARSERS.get(ptype)
    if fn is None:
        return [], {}, JobError(job.id, "config", "high", f"Unknown parser `{ptype}` in job `{job.id}`.",
                                f"Use one of: {', '.join(sorted(PARSERS))}.", tool=job.tool_name)
    try:
        if exit_code == 0 and job.parser.get("file") and report_text is None:
            raise ParseError(f"its report {job.parser['file']} was not written")
        errs, metrics = fn(job, stdout, stderr, exit_code, root, report_text)
    except ParseError as e:
        # no output can mean "nothing found", but a report that was asked for or that is damaged can't:
        # a scan whose results are unreadable must never pass
        if exit_code == 0 and isinstance(e, NoOutput) and not job.parser.get("file"):
            return [], {}, None
        problem = JobError(job.id, "crash", "high",
                           f"`{job.tool_name}` exited with code {exit_code} and {e}, so Gatewright could not read its results.",
                           "The tool probably crashed or was called with wrong arguments; read the raw output below.",
                           raw=(stdout + "\n" + stderr)[-MAX_RAW:], tool=job.tool_name)
        return [], {}, problem
    except (KeyError, TypeError, AttributeError, ValueError) as e:
        problem = JobError(job.id, "crash", "high",
                           f"The `{ptype}` parser could not understand the output of `{job.tool_name}` ({e}).",
                           "The tool's output format may have changed; check the tool version.",
                           raw=(stdout + "\n" + stderr)[-MAX_RAW:], tool=job.tool_name)
        return [], {}, problem
    for e in errs:
        finalize(e, root)
    return errs, metrics, None
