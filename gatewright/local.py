# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Local builds: run the gates on a folder, an archive or a file on this computer (no git host).

    resolve(text)    what the user typed or picked -> an existing path
    prepare(...)     -> LocalBuild: which pipeline, where the code is, where results go

Every subject is kept separate (docs/architecture.md): its runs, history, cache and reports live
in its own folder under `state_dir()`, never in the subject itself and never mixed with another
project's. The one exception is a folder that is opened as a project of its own (mode `own`): that is
the same as starting Gatewright there, with its secrets and settings. It has to be asked for.

Other subjects run like an untrusted commit: without secrets, and without the deploy, signing,
notification and git host settings of any project. Archives are unpacked defensively: nothing is
written outside the target folder, links are not created, and sizes are limited.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from . import templates
from .config import GATEWRIGHT_DIR, load_pipeline, load_yaml_text
from .locks import Locked, file_lock
from .models import Pipeline
from .platform_info import detect as detect_os
from .watch import remove_tree
from .winsupport import long_path

MODES = ("auto", "own", "project", "template")
MAX_BYTES = 2 * 1024**3       # unpacked size of an archive
MAX_FILES = 100_000
TAR = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
ZIP = (".zip", ".whl")
# settings of a project that must not act on somebody else's code
PROJECT_ONLY = ("vcs", "signing", "environments", "notify")
WINDOWS_RESERVED = re.compile(r"(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.I)


class LocalError(Exception):
    """The subject can't be used. The message says why, for humans."""


@dataclass
class LocalBuild:
    """A subject that is ready to run: Engine(pipeline, data_root=data_root, trusted=trusted)."""
    path: Path             # what the user chose
    pipeline: Pipeline     # .root is where the code is
    data_root: str         # where runs, history and cache go
    trusted: bool          # False: no secrets
    mode: str              # own | project | template
    note: str              # one line for the user


# ---------------------------------------------------------------- paths
def state_dir() -> Path:
    """Per-user folder for the results of local builds."""
    if os.name == "nt":  # pragma: no cover - Windows only
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    else:
        base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "gatewright" / "local"


def clean_text(text: str, wsl: bool = False) -> str:
    """A path as people paste it (quotes, spaces, file:// URLs, Windows paths in WSL) -> a plain path."""
    t = text.strip().replace("\0", "")
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        t = t[1:-1].strip()
    if t.lower().startswith("file://"):
        from urllib.parse import unquote
        t = unquote(t[7:])
        if re.match(r"/[A-Za-z]:", t):
            t = t[1:]
    m = re.match(r"([A-Za-z]):[\\/](.*)", t)
    if m and wsl:
        t = f"/mnt/{m.group(1).lower()}/" + m.group(2).replace("\\", "/")
    return os.path.expandvars(os.path.expanduser(t))


def resolve(text: str) -> Path:
    """The existing folder or file the user means. Raises LocalError with a reason."""
    t = clean_text(text, sys.platform.startswith("linux") and detect_os().wsl)
    if not t:
        raise LocalError("Enter the full path of a folder, an archive or a file.")
    if not os.path.isabs(t):
        raise LocalError(f"{t} is not a full path. Example: " +
                         (r"C:\work\my-build" if os.name == "nt" else "/home/me/my-build"))
    try:
        path = Path(t).resolve(strict=True)
        readable = os.access(path, os.R_OK)
        regular = path.is_dir() or path.is_file()
    except (OSError, RuntimeError, ValueError) as e:     # RuntimeError: a loop of links (Python < 3.13)
        missing = isinstance(e, FileNotFoundError)
        raise LocalError(f"{t} does not exist." if missing else f"{t} can't be opened: {e}") from None
    if not regular:
        raise LocalError(f"{path} is neither a folder nor a file.")
    if not readable:
        raise LocalError(f"You are not allowed to read {path}.")
    if path.is_dir() and (path == Path(path.anchor) or path == Path.home().resolve()):
        raise LocalError(f"{path} is too broad: the scanners would go through everything in it. Choose a project folder.")
    state = state_dir().resolve()
    if path == state or state in path.parents or path in state.parents:
        raise LocalError(f"{path} contains Gatewright's own results. Choose another folder.")
    return path


def kind_of(path: Path) -> str:
    """folder | zip | tar | file"""
    name = path.name.lower()
    if path.is_dir():
        return "folder"
    return "zip" if name.endswith(ZIP) else "tar" if name.endswith(TAR) else "file"


def store_for(path: Path) -> Path:
    """This subject's own folder for results: the same subject always gets the same one."""
    key = hashlib.sha256(os.path.normcase(str(path)).encode("utf-8", "surrogatepass")).hexdigest()[:12]
    slug = re.sub(r"[^A-Za-z0-9]+", "-", path.name).strip("-")[:40] or "build"
    return state_dir() / f"{slug}-{key}"


# ---------------------------------------------------------------- archives
def _safe_name(name: str) -> PurePosixPath | None:
    """The relative path of an archive member; None for the archive's own root. Raises LocalError
    for anything that could end up somewhere else, on any operating system."""
    raw = name.replace("\\", "/")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    bad = raw.startswith("/") or ".." in parts or any(
        ":" in p or "\0" in p or any(ord(c) < 32 for c in p) or p != p.rstrip(". ") or WINDOWS_RESERVED.match(p)
        for p in parts)
    if bad:
        raise LocalError(f"The archive contains an unsafe path ({name[:80]!r}); nothing from it is used.")
    return PurePosixPath(*parts) if parts else None


class _Budget:
    """Counts what is really written: sizes declared in an archive can lie."""

    def __init__(self) -> None:
        self.bytes = self.files = 0

    def file(self) -> None:
        self.files += 1
        if self.files > MAX_FILES:
            raise LocalError(f"The archive has more than {MAX_FILES} entries.")

    def copy(self, src, target: Path, executable: bool) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as out:
            while chunk := src.read(1 << 20):
                self.bytes += len(chunk)
                if self.bytes > MAX_BYTES:
                    raise LocalError(f"The archive unpacks to more than {MAX_BYTES // 1024**2} MB.")
                out.write(chunk)
        if executable and os.name == "posix":
            target.chmod(0o755)  # nosec B103 - a program in the archive, like unzip/tar would do


def _unzip(path: Path, dest: Path, budget: _Budget) -> int:
    skipped = 0
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            budget.file()
            name, mode = _safe_name(info.filename), info.external_attr >> 16
            if name is None or info.is_dir():
                continue
            if stat.S_ISLNK(mode):
                skipped += 1
                continue
            with z.open(info) as src:
                budget.copy(src, dest / name, bool(mode & 0o100))
    return skipped


def _untar(path: Path, dest: Path, budget: _Budget) -> int:
    skipped = 0
    with tarfile.open(path, "r:*") as t:
        for m in t:
            budget.file()
            name = _safe_name(m.name)
            if name is None or m.isdir():
                continue
            src = t.extractfile(m) if m.isreg() else None      # links, devices, pipes: never created
            if src is None:
                skipped += 1
                continue
            with src:
                budget.copy(src, dest / name, bool(m.mode & 0o100))
    return skipped


def unpack(path: Path, dest: Path) -> tuple[Path, int]:
    """Unpack (or copy a single file) into the empty folder `dest`.
    Returns (where the code is, number of links that were left out)."""
    dest = long_path(dest)
    dest.mkdir(parents=True)
    budget = _Budget()
    kind = kind_of(path)
    try:
        if kind == "file":
            with open(path, "rb") as src:
                budget.copy(src, dest / path.name, os.access(path, os.X_OK))
            return dest, 0
        skipped = (_unzip if kind == "zip" else _untar)(path, dest, budget)
    except LocalError:
        raise
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, NotImplementedError, RuntimeError, ValueError, OSError,
            MemoryError, ArithmeticError) as e:     # RuntimeError: encrypted; others: damaged in some way
        raise LocalError(f"{path.name} could not be unpacked: {e}") from None
    except Exception as e:  # zlib.error, lzma.LZMAError, struct.error, ...: damaged archives
        raise LocalError(f"{path.name} is damaged: {e}") from None
    entries = list(dest.iterdir())
    if not entries:
        raise LocalError(f"{path.name} contains no files.")
    # source archives usually have one top folder (name-1.0/)
    return (entries[0] if len(entries) == 1 and entries[0].is_dir() else dest), skipped


# ---------------------------------------------------------------- pipelines
def _adopt(source: Path, store: Path) -> None:
    """Copy a project's pipeline into the store, without the settings that belong to that project."""
    data = load_yaml_text((source / GATEWRIGHT_DIR / "pipeline.yml").read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = {k: v for k, v in data.items() if k not in PROJECT_ONLY}
    (store / GATEWRIGHT_DIR / "pipeline.yml").write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                                                         encoding="utf-8")
    jobs = source / GATEWRIGHT_DIR / "jobs"
    for f in sorted([*jobs.glob("*.yml"), *jobs.glob("*.yaml")]):
        (store / GATEWRIGHT_DIR / "jobs" / f.name).write_bytes(f.read_bytes())


def has_pipeline(folder: Path) -> bool:
    return (folder / GATEWRIGHT_DIR / "pipeline.yml").is_file()


def prepare(path: Path, mode: str = "auto", project: Pipeline | None = None) -> LocalBuild:
    """Make a subject ready to run. `project`: the project Gatewright is open in (for mode `project`).

    auto = the jobs of the subject's own pipeline if it has one, else the template that fits its files.
    Raises LocalError, or ConfigError for an invalid pipeline.
    """
    if mode not in MODES:
        raise LocalError(f"Unknown pipeline choice `{mode}`. Use one of: {', '.join(MODES)}.")
    if mode == "project" and project is None:
        raise LocalError("There is no current project whose pipeline could be used.")
    kind = kind_of(path)
    if kind == "folder" and mode == "own" and has_pipeline(path):
        return LocalBuild(path, load_pipeline(path), str(path), True, "own", "its own pipeline, as a project of its own")

    store = store_for(path)
    store.mkdir(parents=True, exist_ok=True)
    try:
        # two windows preparing the same subject would delete each other's files
        with file_lock(store / "prepare.lock"):
            return _prepare(path, kind, mode, project, store)
    except Locked:
        raise LocalError(f"{path.name} is being prepared by another Gatewright. Try again in a moment.") from None


def _prepare(path: Path, kind: str, mode: str, project: Pipeline | None, store: Path) -> LocalBuild:
    skipped = 0
    root = path
    if kind != "folder":
        src = store / "src"
        new = store / "src.new"
        _remove(new)
        try:
            inner, skipped = unpack(path, new)
        except LocalError:
            _remove(new)
            raise
        rel = os.path.relpath(inner, long_path(new))
        _remove(src)
        try:
            os.replace(long_path(new), long_path(src))
        except OSError as e:
            raise LocalError(f"The previous copy of {path.name} is still in use ({e}). Is a run still going?") from None
        root = src if rel == "." else src / rel
    if mode == "auto":
        mode = "own" if has_pipeline(root) else "template"
    if mode == "own" and not has_pipeline(root):
        raise LocalError(f"{path.name} has no pipeline of its own ({GATEWRIGHT_DIR}/pipeline.yml).")

    _remove(store / GATEWRIGHT_DIR / "jobs")
    (store / GATEWRIGHT_DIR / "jobs").mkdir(parents=True)
    if mode == "template":
        name = templates.detect_template(root)
        templates.write(store, name, path.name)
        what = f"the {name} template"
    else:
        _adopt(root if mode == "own" else Path(project.root), store)
        what = "its own pipeline" if mode == "own" else f"the pipeline of {project.name}"
    pipe = load_pipeline(store)
    pipe.root = str(root)
    note = f"{what}, without secrets"
    if skipped:
        note += f"; {skipped} link(s) in the archive were left out"
    return LocalBuild(path, pipe, str(store), False, mode, note)


def _remove(path: Path) -> None:
    if not path.exists():
        return
    try:
        remove_tree(path)
    except OSError as e:
        raise LocalError(f"{path} could not be replaced ({e}). Is a run of it still going?") from None
