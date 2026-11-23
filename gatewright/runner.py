# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Running one job: process management, streaming logs, timeouts, retries,
and turning the outcome into a JobResult.

`execute()` is the heart of this module. For one job it:

  1. checks the tool exists (`tool_available`) - optional jobs are SKIPPED,
     required ones become an INFRA error with an install hint
  2. resolves secrets and builds the environment (PATH order: see `job_path`)
  3. expands Gatewright's variables ($GATEWRIGHT_OUT, %GATEWRIGHT_OUT%, ...) so the same job
     file works in sh, bash, cmd.exe and PowerShell
  4. starts the process (host shell via `shell_command`, or `docker run` via
     `_docker_argv`), streams stdout/stderr line by line into the log
     (secret-masked) and to the `on_log` callback
  5. samples memory/CPU of the whole process tree, enforces the timeout
  6. classifies infrastructure failures (errors.classify_infra) and retries
     the retryable ones (timeout, network) with backoff
  7. parses the tool's output into JobErrors (errors.parse_output), applies
     baseline suppressions, and decides PASSED / WARN / FAILED / ERROR

Stopping a job (timeout, cancel, memory watchdog) always goes through
`_terminate`, which kills the container and the *whole* process tree, on
Linux, macOS and Windows.
"""

from __future__ import annotations

import functools
import os
import sys
import re
import shlex
import shutil
import signal
import subprocess  # nosec B404
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import psutil

from .errors import classify_infra, finalize_all, parse_output
from .hardware import process_tree_usage
from .models import (ERROR, FAILED, PASSED, SEVERITY_RANK, SKIPPED, WARN, Job, JobError,
                     JobResult, plain)
from .secrets import SecretStore
from .winsupport import OWN_GROUP, long_path, request_stop

STOP_GRACE = 3.0   # seconds a job gets to clean up after a stop request (Windows)
RETRYABLE = {"timeout", "infra"}
MAX_CAPTURE = 64 * 2**20   # bytes of stdout/stderr kept in memory per stream (for parsers)
SHELL_BUILTINS = {"sh", "bash", "cmd", "echo", "true", "false", "test", "[", "cd", "export", "exit", "set",
                  "source", ".", ":", "printf", "if", "for", "while", "case", "{", "(", "env", "exec"}
LogFn = Callable[[str, str], None]
# Commands built into cmd.exe (no .exe exists, so `which` can't find them)
CMD_BUILTINS = {"assoc", "call", "cd", "chdir", "cls", "copy", "date", "del", "dir", "echo", "endlocal", "erase",
                "exit", "for", "ftype", "goto", "if", "md", "mkdir", "mklink", "move", "path", "pause", "popd",
                "prompt", "pushd", "rd", "rem", "ren", "rename", "rmdir", "set", "setlocal", "shift", "start",
                "time", "title", "type", "ver", "verify", "vol"}
POWERSHELL_CMDLET = re.compile(r"^[A-Z][a-z]+-[A-Z][A-Za-z]+$")  # Get-Content, Invoke-WebRequest, ...


def gatewright_tools_dir() -> str | None:
    """bin/Scripts dir of the environment Gatewright runs in, if it is a venv
    (install.sh puts ruff/bandit/pytest/... there)."""
    if sys.prefix == sys.base_prefix:
        return None
    return os.path.dirname(sys.executable)


def project_venv(root: str | None) -> str | None:
    """bin/Scripts dir of the project's own virtualenv (.venv, venv or env), if any."""
    if not root:
        return None
    for name in (".venv", "venv", "env"):
        d = os.path.join(root, name)
        if os.path.isfile(os.path.join(d, "pyvenv.cfg")):
            for sub in ("bin", "Scripts"):
                if os.path.isdir(os.path.join(d, sub)):
                    return os.path.join(d, sub)
    return None


_SHIMS: str | None = None
_SHIMS_LOCK = threading.Lock()


def shim_files(name: str, python: str, os_name: str) -> list[tuple[str, str]]:
    """(file name, content) of the wrappers that make `name` start `python`.

    Windows needs two: a sh script for Git Bash, and a .cmd for cmd.exe and PowerShell.
    """
    target = python.replace("\\", "/") if os_name == "nt" else python
    files = [(name, f'#!/bin/sh\nexec {shlex.quote(target)} "$@"\n')]
    if os_name == "nt":
        batch = python.replace("%", "%%")
        files.append((f"{name}.cmd", f'@"{batch}" %*\r\n'))
    return files


def _python_shims() -> str | None:
    with _SHIMS_LOCK:   # a watcher with `parallel:` calls this from several threads
        return _make_python_shims()


def _make_python_shims() -> str | None:
    """A folder where `python`/`python3` point at Gatewright's own interpreter, used
    when the project has no venv, so `python -m pytest` finds the tools that
    install.sh put next to Gatewright. Other commands still resolve from your PATH first."""
    global _SHIMS
    tools = gatewright_tools_dir()
    if tools is None:
        return None
    if _SHIMS is None:
        import tempfile
        import atexit
        d = tempfile.mkdtemp(prefix="gatewright-shims-")
        atexit.register(shutil.rmtree, d, True)   # don't leave temp folders behind (known issue L9)
        for n in ("python", "python3"):
            # a wrapper, not a symlink: Python only finds its venv (pyvenv.cfg) when
            # started through the venv's own path
            for name, text in shim_files(n, sys.executable, os.name):
                shim = os.path.join(d, name)
                with open(shim, "w", encoding="utf-8", newline="") as fh:
                    fh.write(text)
                os.chmod(shim, 0o755)  # nosec B103 - private temp dir, executable wrapper
        _SHIMS = d
    return _SHIMS


def job_path(root: str | None = None, base: str | None = None) -> str:
    """PATH for jobs:
        1. the project's venv (.venv/venv/env) if it has one, else Gatewright's python shims
        2. your PATH
        3. Gatewright's own tools (ruff, bandit, ... installed by install.sh)
    """
    base = os.environ.get("PATH", "") if base is None else base
    parts = [p for p in base.split(os.pathsep) if p]
    front = project_venv(root) or _python_shims()
    out = ([front] if front else []) + [p for p in parts if p != front]
    tools = gatewright_tools_dir()
    if tools and tools not in out:
        out.append(tools)
    return os.pathsep.join(out)


def python_for_jobs(root: str | None) -> tuple[str | None, str]:
    """(interpreter, where it comes from) that `python` resolves to in jobs."""
    exe = shutil.which("python", path=job_path(root)) or shutil.which("python3", path=job_path(root))
    if project_venv(root) and exe and os.path.dirname(exe) == project_venv(root):
        return exe, "project venv"
    if exe and _SHIMS and os.path.dirname(exe) == _SHIMS:
        return sys.executable, "Gatewright's environment (project has no .venv)"
    return exe, "PATH"


def which(name: str, root: str | None = None) -> str | None:
    """shutil.which() using the PATH that jobs will get (see `job_path`).
    For one of Gatewright's python shims: the interpreter it starts."""
    exe = shutil.which(name, path=job_path(root))
    return sys.executable if exe and _SHIMS and os.path.dirname(exe) == _SHIMS else exe


_TTL = 300.0   # seconds; long-running processes (TUI, watcher) notice tool upgrades
_ttl_cache: dict[tuple, tuple[float, object]] = {}


def _ttl(fn):
    """Like functools.lru_cache, but entries expire after _TTL seconds."""
    @functools.wraps(fn)
    def wrapper(*args):
        key = (fn.__name__, *args)
        hit = _ttl_cache.get(key)
        if hit and time.monotonic() - hit[0] < _TTL:
            return hit[1]
        value = fn(*args)
        _ttl_cache[key] = (time.monotonic(), value)
        return value
    wrapper.cache_clear = _ttl_cache.clear  # type: ignore[attr-defined]
    return wrapper


@_ttl
def _has_module(python: str, module: str) -> bool:
    try:
        return subprocess.run(  # nosec B603 - fixed argv
            [python, "-c", f"import importlib.util, sys; sys.exit(0 if importlib.util.find_spec({module!r}) else 1)"],
            capture_output=True, timeout=20, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@_ttl
def tool_version(tool: str, root: str | None = None) -> str:
    """First line of `<tool> --version` (cached). Part of the cache key and the report."""
    exe = which(tool, root)
    if not exe:
        return ""
    for flag in ("--version", "version", "-V"):
        try:
            out = subprocess.run([exe, flag], capture_output=True, text=True, timeout=10,  # nosec B603
                                 check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        text = (out.stdout or out.stderr).strip().splitlines()
        if out.returncode == 0 and text:
            return text[0][:120]
    return "unknown"


def _python_module(job: Job) -> tuple[str, str] | None:
    """('python', 'pytest') for `python -m pytest ...`."""
    parts = job.run.split()
    if len(parts) >= 3 and parts[0] in ("python", "python3") and parts[1] == "-m":
        return parts[0], parts[2]
    return None


def tool_available(job: Job, root: str | None = None) -> bool:
    """Can this job's tool be started on this machine?

    For `python -m X` the module X must be importable by the Python the job will
    use (project venv, Gatewright's own, or PATH) - not merely exist somewhere.
    Container jobs only need the docker CLI here; the daemon is checked at run time.
    """
    if job.image:
        return shutil.which("docker") is not None
    pm = _python_module(job)
    if pm:  # `python -m X`: X must be importable by the python the job will use
        py = which(pm[0], root)
        return bool(py) and _has_module(py, pm[1])
    name = job.tool_name
    if not name or name in SHELL_BUILTINS or "=" in name:
        return True
    _, _, shell = shell_command(job.run, job.shell)
    if shell == "cmd" and name.lower() in CMD_BUILTINS:
        return True
    if shell == "powershell" and POWERSHELL_CMDLET.match(name):
        return True
    if which(name, root) is not None:
        return True
    if shell == "bash" and os.name == "nt":  # pragma: no cover - Windows only
        # Git Bash puts its own /usr/bin and /mingw64/bin (sleep, cat, grep, ...) on PATH
        # when it starts; they are not on the Windows PATH that `which` searches.
        extra = os.pathsep.join(git_bash_tool_dirs(_windows_bash()))
        return bool(extra) and shutil.which(name, path=extra) is not None
    return False


def git_bash_tool_dirs(bash: str | None) -> list[str]:
    """Folders Git Bash adds to PATH (usr/bin, mingw64/bin), found from its bash.exe."""
    if not bash:
        return []
    d = os.path.dirname(os.path.abspath(bash))
    # <Git>\bin\bash.exe (launcher) or <Git>\usr\bin\bash.exe (the real one)
    git_root = os.path.dirname(os.path.dirname(d)) if os.path.basename(os.path.dirname(d)).lower() == "usr" \
        else os.path.dirname(d)
    return [p for p in (os.path.join(git_root, "usr", "bin"), os.path.join(git_root, "mingw64", "bin"))
            if os.path.isdir(p)]


class JobHandle:
    """A running job. The scheduler's watchdog may call kill()."""

    def __init__(self, job: Job):
        self.job = job
        self.proc: subprocess.Popen | None = None
        self.kill_reason = ""
        self.started_at = time.monotonic()
        self.container: str | None = None
        self.rss = 0

    def kill(self, reason: str = "") -> None:
        """Stop the job (process tree and container). The first reason given is kept
        and reported to the user as a resource/cancel error.
        """
        if reason and not self.kill_reason:
            self.kill_reason = reason
        _terminate(self.proc, self.container)


def _kill_tree(proc: subprocess.Popen) -> None:
    """Stop a process and everything it started. Works on Windows too, where
    killing the shell (cmd/bash) would otherwise leave its children running."""
    try:
        parent = psutil.Process(proc.pid)
        family = parent.children(recursive=True) + [parent]
    except psutil.Error:
        family = []
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    elif request_stop(proc):   # terminate() is immediate on Windows: let tools clean up first
        psutil.wait_procs(family, timeout=STOP_GRACE)
    for p in family:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(family, timeout=3)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _terminate(proc: subprocess.Popen | None, container: str | None) -> None:
    if container and shutil.which("docker"):
        try:
            subprocess.run(["docker", "kill", container], capture_output=True, check=False, timeout=15)  # nosec
        except (OSError, subprocess.SubprocessError):
            pass   # a hung docker CLI must never crash the scheduler or the watchdog
    if proc is None or proc.poll() is not None:
        return
    _kill_tree(proc)


CANCEL_REASON = "run cancelled by user"   # JobHandle.kill() reason used when the user stops a run

GATEWRIGHT_VARS = ("GATEWRIGHT_OUT", "GATEWRIGHT_RUN_ID", "GATEWRIGHT_JOB", "GATEWRIGHT_GATE")


def expand_gatewright_vars(text: str, values: dict[str, str]) -> str:
    """Substitute Gatewright's own variables in a command so the same job file works in
    sh, bash, cmd.exe and PowerShell ($X, ${X}, %X% and $env:X)."""
    for k in sorted(values, key=len, reverse=True):  # longest first: GATEWRIGHT_OUT_X before GATEWRIGHT_OUT
        v = values[k]
        if v is None or not k.startswith("GATEWRIGHT_"):
            continue
        for pat in (f"${{{k}}}", f"$env:{k}", f"%{k}%"):
            text = text.replace(pat, v)
        text = re.sub(rf"\${k}(?![A-Za-z0-9_])", lambda m, v=v: v, text)
    return text


def _windows_bash(which=shutil.which) -> str | None:
    """Git Bash, not WSL's System32\\bash.exe (which runs in a different filesystem)."""
    cand = which("bash")
    if cand and "system32" not in cand.lower() and "windowsapps" not in cand.lower():
        return cand
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        p = os.path.join(base, "Git", "bin", "bash.exe")
        if os.path.exists(p):
            return p
    return None


def shell_command(run: str, shell: str = "auto", os_name: str | None = None,
                  which=shutil.which) -> tuple[list[str] | str, bool, str]:
    """Return (args, use_shell, shell_name) for subprocess.Popen."""
    os_name = os_name or os.name
    if shell == "auto":
        if os_name != "nt":
            shell = "sh"
        else:
            shell = "bash" if _windows_bash(which) else "cmd"
    if shell == "sh":
        return run, True, "sh"
    if shell == "bash":
        exe = (_windows_bash(which) if os_name == "nt" else which("bash")) or "bash"
        return [exe, "-c", run], False, "bash"
    if shell == "powershell":
        exe = which("pwsh") or which("powershell") or "powershell"
        return [exe, "-NoProfile", "-NonInteractive", "-Command", run], False, "powershell"
    if shell == "cmd":
        if os_name == "nt":
            return run, True, "cmd"
        return run, True, "sh"
    raise ValueError(f"unknown shell {shell!r}")


CONTAINER_ROOT = "/work"
CONTAINER_OUT = "/gatewright-out"


def _inside(path: str, root: str) -> bool:
    try:
        return Path(os.path.relpath(path, root)).parts[:1] != ("..",)
    except ValueError:  # another Windows drive
        return False


@_ttl
def container_engine() -> str:
    """What the `docker` command is: docker, rootless (Docker) or podman (rootless)."""
    # podman calls itself docker when started under that name, but only it has these fields
    for fmt, word, engine in (("{{.Host.Security.Rootless}}", "true", "podman"),
                              ("{{.SecurityOptions}}", "name=rootless", "rootless")):
        try:
            out = subprocess.run(["docker", "info", "--format", fmt], capture_output=True, text=True,  # nosec B603 B607
                                 timeout=15, check=False)
        except (OSError, subprocess.SubprocessError):
            break
        if out.returncode == 0 and word in out.stdout:
            return engine
    return "docker"


def _docker_argv(job: Job, root: str, run_id: str, out_dir: str, secret_names: list[str],
                 attempt: int = 1) -> tuple[list[str], str]:
    safe = re.sub(r"[^a-z0-9-]", "-", f"{job.id}".lower())
    # shorten the job id, never the attempt number: retries need distinct names
    keep = max(1, 63 - len(f"gatewright-{run_id}--{attempt}"))
    name = f"gatewright-{run_id}-{safe[:keep]}-{attempt}"
    mounts = ["-v", f"{root}:{CONTAINER_ROOT}"]
    if _inside(out_dir, root):
        out = f"{CONTAINER_ROOT}/" + os.path.relpath(out_dir, root).replace(os.sep, "/")
    else:   # the watcher runs checkouts, but keeps run folders in the home project
        out = CONTAINER_OUT
        mounts += ["-v", f"{out_dir}:{CONTAINER_OUT}"]
    argv = ["docker", "run", "--rm", "--name", name,
            "--cpus", str(job.cpu), "--memory", str(job.mem),
            *mounts, "-w", CONTAINER_ROOT, "-e", f"GATEWRIGHT_OUT={out}",
            "-e", f"GATEWRIGHT_RUN_ID={run_id}", "-e", f"GATEWRIGHT_JOB={job.id}", "-e", f"GATEWRIGHT_GATE={job.gate}"]
    if job.container_user == "host" and hasattr(os, "getuid") and os.getuid() != 0:
        engine = container_engine()      # files written to the project stay yours
        if engine == "podman":           # rootless: your uid inside is another one outside, unless kept
            argv.append("--userns=keep-id")
        if engine != "rootless":         # rootless Docker: root inside already is you outside
            argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
    if not job.network:
        argv += ["--network", "none"]
    for k in job.env:
        argv += ["-e", k]
    for s in secret_names:
        argv += ["-e", s]  # value is inherited from our env, never on the command line
    argv += [job.image, "sh", "-c", job.run]
    return argv, name


MASK_FILE_LIMIT = 32 * 1024 * 1024


def mask_output_files(out_dir: str, secrets: SecretStore) -> int:
    """Mask secret values inside the text files a job wrote to $GATEWRIGHT_OUT (they become
    part of the artifact). Binary and very large files are left alone. Returns files changed."""
    if not secrets.active:
        return 0
    changed = 0
    for path in long_path(out_dir).rglob("*"):
        try:
            if not path.is_file() or path.is_symlink() or path.stat().st_size > MASK_FILE_LIMIT:
                continue
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue                      # binary or unreadable: not a text report
        masked = secrets.mask(text)
        if masked != text:
            path.write_bytes(masked.encode("utf-8"))
            changed += 1
    return changed


def _read_stream(stream, sink: list[str], log_fh, lock: threading.Lock, on_log: LogFn | None,
                 job_id: str, mask: Callable[[str], str], prefix: str) -> None:
    kept = 0
    for line in iter(stream.readline, ""):
        if kept < MAX_CAPTURE:          # the log file gets everything; memory keeps the first MAX_CAPTURE bytes
            sink.append(line)
            kept += len(line)
            if kept >= MAX_CAPTURE:
                sink.append(f"\n[gatewright: output beyond {MAX_CAPTURE // 2**20} MB not kept in memory; see the log]\n")
        masked = mask(plain(line.rstrip("\n")))    # in this order: an escape inside a secret must not hide it
        with lock:
            log_fh.write(f"{prefix}{masked}\n")
            log_fh.flush()
        if on_log:
            on_log(job_id, masked)
    stream.close()


def job_environ(job: Job, trusted: bool) -> dict[str, str]:
    """The environment jobs inherit: yours, without Gatewright's secrets. A job gets the ones in its
    `secrets:` list under their own names; an untrusted job must not find any."""
    hidden = set() if trusted else {*(s.upper() for s in job.secrets), "VCS_TOKEN", "VCS_WEBHOOK_SECRET", "GH_TOKEN", "GITHUB_TOKEN",
                                    "GITLAB_TOKEN", "SSH_AUTH_SOCK"}
    return {k: v for k, v in os.environ.items()
            if not k.upper().startswith("GATEWRIGHT_SECRET_") and k.upper() not in hidden}


def execute(job: Job, *, root: str, run_id: str, out_dir: str, log_file: str,
            secrets: SecretStore, handle: JobHandle, on_log: LogFn | None = None,
            suppress: Callable[[JobError], None] | None = None, nice: int = 0) -> JobResult:
    """Run one job to completion and return its JobResult. Never raises for job
    problems: everything (missing tool, timeout, crash, findings) becomes a status
    plus JobErrors. See the module docstring for the step-by-step flow.

    `suppress` is called for each finding (the baseline), `nice` lowers the
    job's CPU priority on laptops so the machine stays responsive.
    """
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    t0 = time.monotonic()
    job_started_wall = time.time()
    result = JobResult(job.id, job.gate, job.category, PASSED, started=started, log_file=log_file)
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    def done(status: str, errors: list[JobError] | None = None, note: str = "") -> JobResult:
        result.status = status
        result.errors = finalize_all(list(errors or []), root)
        result.note = note
        result.duration = round(time.monotonic() - t0, 2)
        return result

    if job.when_exists:
        import glob as _glob
        if not any(_glob.glob(os.path.join(root, pat), recursive=True) for pat in job.when_exists):
            Path(log_file).write_text(f"skipped: none of {job.when_exists} exist\n", encoding="utf-8")
            return done(SKIPPED, note=f"skipped: no {', '.join(job.when_exists)} in the project")

    if not tool_available(job, root):
        if job.optional:
            Path(log_file).write_text(f"skipped: `{job.tool_name}` is not installed (job is optional)\n",
                                      encoding="utf-8")
            return done(SKIPPED, note=f"optional tool `{job.tool_name}` not installed")
        # a container job needs docker here; its own command is looked up inside the image
        needed = Job(job.id, job.gate, "docker", tool="docker") if job.image else job
        err = classify_infra(needed, 127, "", False)
        Path(log_file).write_text(f"error: `{needed.tool_name}` is not installed\n", encoding="utf-8")
        return done(ERROR, [err])

    secret_env, missing = secrets.resolve(job.secrets)
    if missing and not secrets.enabled:
        return done(ERROR, [JobError(
            job.id, "config", "high",
            f"Secrets are withheld from this run because it is untrusted (e.g. a pull request from a fork), "
            f"and this job needs: {', '.join(missing)}.",
            "Review the change; if it's safe, run it from a trusted branch, or make the job not need secrets.",
            tool=job.tool_name)])
    if missing:
        return done(ERROR, [JobError(
            job.id, "config", "high",
            f"The job needs the secret(s) {', '.join(missing)} but they are not set.",
            f"Set them as environment variables (e.g. GATEWRIGHT_SECRET_{missing[0]}) or add them to .gatewright/secrets.env.",
            tool=job.tool_name)])

    result.tool_version = tool_version(job.tool_name, root) if not job.image else job.image

    env = {**job_environ(job, secrets.enabled), "PATH": job_path(root), **job.env, **secret_env, "GATEWRIGHT_OUT": out_dir, "GATEWRIGHT_RUN_ID": run_id,
           "GATEWRIGHT_JOB": job.id, "GATEWRIGHT_GATE": job.gate, "PYTHONUNBUFFERED": "1", "NO_COLOR": "1",
           # Python tools print UTF-8 even where the console uses a legacy code page (Windows cp850/cp1252)
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

    out_dir_fwd = out_dir.replace("\\", "/")  # forward slashes work in every Windows shell too
    run_text = job.run if job.image else expand_gatewright_vars(
        job.run, {**{k: v for k, v in job.env.items() if k.startswith("GATEWRIGHT_")},
                  "GATEWRIGHT_OUT": out_dir_fwd, "GATEWRIGHT_RUN_ID": run_id, "GATEWRIGHT_JOB": job.id,
                  "GATEWRIGHT_GATE": job.gate})
    attempts = max(1, job.retries + 1)
    stdout = stderr = ""
    exit_code: int | None = None
    infra: JobError | None = None
    infra_from_pattern = False     # True: infra was only guessed from words in the output
    timed_out = False

    with open(log_file, "w", encoding="utf-8") as log_fh:
        lock = threading.Lock()
        log_fh.write(f"$ {secrets.mask(run_text)}\n")
        for attempt in range(1, attempts + 1):
            result.attempts = attempt
            if attempt > 1:
                wait = min(30, 2 ** (attempt - 1))
                msg = f"--- retry {attempt}/{attempts} in {wait}s ({infra.category if infra else ''}) ---"
                log_fh.write(msg + "\n")
                if on_log:
                    on_log(job.id, msg)
                end = time.monotonic() + wait
                while time.monotonic() < end and not handle.kill_reason:   # cancel/watchdog stop the wait
                    time.sleep(0.1)
                if handle.kill_reason:
                    break
            out_lines: list[str] = []
            err_lines: list[str] = []
            if job.image:
                argv, handle.container = _docker_argv(job, root, run_id, out_dir, list(secret_env), attempt)
                popen_args = {"args": argv}
            else:
                args, use_shell, _ = shell_command(run_text, job.shell)
                popen_args = {"args": args, "shell": use_shell}  # nosec B602 - job commands are user-defined
            kwargs = {}
            if os.name == "posix":
                kwargs["start_new_session"] = True
                if nice:
                    kwargs["preexec_fn"] = lambda n=nice: os.nice(n)  # noqa: E731
            else:  # pragma: no cover - Windows only
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            try:
                proc = subprocess.Popen(  # nosec B603
                    **popen_args, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1, **kwargs)
            except OSError as e:
                infra = JobError(job.id, "crash", "high", f"Gatewright could not start the job: {e}",
                                 "Check the `run:` command and the working directory.", raw=str(e),
                                 tool=job.tool_name)
                break
            if os.name == "nt" and nice:  # pragma: no cover - Windows only (POSIX uses os.nice above)
                try:
                    psutil.Process(proc.pid).nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
                except (psutil.Error, AttributeError):
                    pass
            setattr(proc, OWN_GROUP, True)   # see winsupport.request_stop
            handle.proc = proc
            if handle.kill_reason:      # stop was requested while the process was starting
                _terminate(proc, handle.container)
            threads = [
                threading.Thread(target=_read_stream, daemon=True,
                                 args=(proc.stdout, out_lines, log_fh, lock, on_log, job.id, secrets.mask, "")),
                threading.Thread(target=_read_stream, daemon=True,
                                 args=(proc.stderr, err_lines, log_fh, lock, on_log, job.id, secrets.mask, "")),
            ]
            for t in threads:
                t.start()
            timed_out = False
            deadline = time.monotonic() + job.timeout
            while proc.poll() is None:
                rss, cpu = process_tree_usage(proc.pid)
                handle.rss = rss
                result.peak_mem = max(result.peak_mem, rss)
                result.peak_cpu = max(result.peak_cpu, cpu)
                if time.monotonic() > deadline:
                    timed_out = True
                    _terminate(proc, handle.container)
                    break
                time.sleep(0.2)
            proc.wait()
            for t in threads:
                t.join(timeout=5)
            exit_code = proc.returncode
            stdout, stderr = "".join(out_lines), "".join(err_lines)
            infra = classify_infra(job, exit_code, secrets.mask(stdout + "\n" + stderr), timed_out,
                                   handle.kill_reason)
            infra_from_pattern = infra is not None and classify_infra(
                job, exit_code, "", timed_out, handle.kill_reason, patterns=False) is None
            if infra is None or infra.category not in RETRYABLE or handle.kill_reason:
                break
        log_fh.write(f"--- exit code {exit_code} ---\n")

    result.exit_code = exit_code
    if handle.kill_reason == CANCEL_REASON:
        return done(SKIPPED, note="cancelled")   # not a code or machine problem: the user stopped the run
    if infra is not None and not (infra_from_pattern and job.parser.get("type", "generic") != "generic"):
        if infra.category == "tool_missing" and job.optional:
            return done(SKIPPED, note=infra.human)
        return done(ERROR, [infra])
    # From here on, `infra` (if set) came from text patterns in the output of a job with a structured
    # parser: if the tool's own results are readable they win (e.g. a test that fails with
    # "Permission denied" is a test failure, not a pipeline problem).

    # read report file, if configured
    mask_output_files(out_dir, secrets)   # before parsing and before the artifact is checksummed
    report_text = None
    rfile = job.parser.get("file")
    if rfile:
        rpath = Path(expand_gatewright_vars(rfile, {"GATEWRIGHT_OUT": out_dir, "GATEWRIGHT_RUN_ID": run_id,
                                                "GATEWRIGHT_JOB": job.id, "GATEWRIGHT_GATE": job.gate}))
        if not rpath.is_absolute():
            rpath = Path(root) / rpath
        try:
            # only a report written by THIS job counts (a stale file from an earlier run must not pass for it)
            if rpath.is_file() and rpath.stat().st_mtime >= job_started_wall - 1:
                # masked like the console output: a test report can contain a token too
                report_text = secrets.mask(rpath.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            report_text = None
    stdout_m, stderr_m = secrets.mask(stdout), secrets.mask(stderr)
    if job.image:  # map container paths back to the project so errors point at your files
        host_root = root.rstrip("/\\") + "/"
        stdout_m = stdout_m.replace(CONTAINER_ROOT + "/", host_root)
        stderr_m = stderr_m.replace(CONTAINER_ROOT + "/", host_root)
    errors, metrics, problem = parse_output(job, stdout_m, stderr_m, exit_code or 0, root, report_text)
    result.metrics = metrics
    if infra is not None and (problem or not (errors or metrics.get("tests.total"))):
        return done(ERROR, [infra])    # the tool's results don't explain the failure: the pattern does
    if problem:
        return done(ERROR, [problem])

    # nonzero exit but structured parser found nothing -> generic fallback
    if exit_code != 0 and not errors and not metrics.get("findings.ignored"):
        from .errors import PARSERS
        errors, _ = PARSERS["generic"](job, stdout_m, stderr_m, exit_code, root, None)

    for e in errors:
        e.raw = secrets.mask(plain(e.raw))
        e.human = secrets.mask(plain(e.human))
    if suppress:
        for e in finalize_all(errors, root):
            suppress(e)

    threshold = SEVERITY_RANK.get(job.fail_on, 0)
    blocking = [e for e in errors if not e.suppressed and SEVERITY_RANK.get(e.severity, 0) >= threshold]
    active = [e for e in errors if not e.suppressed]
    if blocking:
        return done(FAILED, errors)
    if active:
        return done(WARN, errors, note=f"{len(active)} issue(s) below fail_on={job.fail_on}")
    if errors:
        return done(PASSED, errors, note=f"{len(errors)} issue(s) suppressed by baseline")
    return done(PASSED, errors)


def describe_command(job: Job) -> str:
    """The command as the user would type it (used by dry-run and the TUI)."""
    if job.image:
        return plain(f"docker run {job.image} sh -c {shlex.quote(job.run)}")
    return plain(job.run)
