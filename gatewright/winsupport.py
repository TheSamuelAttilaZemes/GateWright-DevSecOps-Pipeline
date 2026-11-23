# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Windows-specific helpers. On Linux and macOS they change nothing.

    long_path(p)           paths over 260 characters (docs/windows.md W8)
    true_case(root, rel)   the spelling of a path on disk (W9)
    broad_readers(sddl)    which broad groups may read a file (W10)
    request_stop(proc)     ask a job to stop before it is killed (W6)

The decisions are plain functions, so they are tested on every OS. Only the calls
into the Windows API are Windows only.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess  # nosec B404 - types only, nothing is started here
import sys
from pathlib import Path


# ------------------------------------------------------------------ W8: long paths
def extended(path: str) -> str:
    r"""C:\x -> \\?\C:\x and \\srv\share -> \\?\UNC\srv\share: no 260 character limit."""
    if path.startswith(("\\\\?\\", "\\\\.\\")):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def long_path(path: Path | str) -> Path:
    """The same file, reachable on Windows even when its path is over 260 characters."""
    if sys.platform != "win32":
        return Path(path)
    return Path(extended(os.path.abspath(path)))  # pragma: no cover - Windows only


def long_paths_enabled() -> bool | None:
    """The LongPathsEnabled setting; None where there is no such setting."""
    if sys.platform != "win32":
        return None
    import winreg  # pragma: no cover - Windows only
    try:  # pragma: no cover - Windows only
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            return bool(winreg.QueryValueEx(k, "LongPathsEnabled")[0])
    except OSError:  # pragma: no cover - Windows only
        return False


# ------------------------------------------------------------------ W9: path case
def true_case(root: str, rel: str) -> str:
    """`app/X.PY` -> `App/x.py` if that is how it is spelled on disk below `root`.

    Parts that don't exist keep the spelling they came with.
    """
    here, out = root, []
    for part in rel.split("/"):
        try:
            names = os.listdir(here)
        except OSError:
            names = []
        if part not in names:
            part = next((n for n in names if n.lower() == part.lower()), part)
        out.append(part)
        here = os.path.join(here, part)
    return "/".join(out)


# ------------------------------------------------------------------ W10: file permissions
BROAD = {"WD": "Everyone", "S-1-1-0": "Everyone", "BU": "Users", "S-1-5-32-545": "Users",
         "AU": "Authenticated Users", "S-1-5-11": "Authenticated Users", "BG": "Guests", "S-1-5-32-546": "Guests",
         "AN": "Anonymous", "S-1-5-7": "Anonymous", "IU": "Interactive users", "S-1-5-4": "Interactive users",
         "NU": "Network users", "S-1-5-2": "Network users", "DU": "Domain Users"}
_SID = {name: sid for sid, name in BROAD.items() if sid.startswith("S-")}
_READ_NAMES = {"FA", "FR", "GA", "GR", "CC"}     # CC is bit 0x1: FILE_READ_DATA on a file
_READ_BITS = 0x1 | 0x80000000 | 0x10000000       # FILE_READ_DATA, GENERIC_READ, GENERIC_ALL


def _pairs(text: str) -> list[str]:
    return [text[i:i + 2] for i in range(0, len(text), 2)]


def _grants_read(rights: str) -> bool:
    if rights.lower().startswith("0x"):
        return bool(int(rights, 16) & _READ_BITS)
    return any(r in _READ_NAMES for r in _pairs(rights))


def broad_readers(sddl: str) -> list[str]:
    """Broad groups that may read a file, from its permissions in SDDL form.

    SIDs, not names, so it works in every Windows language. An earlier deny entry for the
    same group wins, as in Windows. A file without a permission list is open to everyone.
    """
    dacl = re.search(r"D:([A-Z_]*)((?:\([^)]*\))*)", sddl)
    if not dacl or "NO_ACCESS_CONTROL" in dacl.group(1):
        return ["Everyone"]
    denied: set[str] = set()
    out: list[str] = []
    for ace in re.findall(r"\(([^)]*)\)", dacl.group(2)):
        kind, flags, rights, _, _, sid = (ace.split(";") + [""] * 6)[:6]
        name = BROAD.get(sid) or ("Domain Users" if re.fullmatch(r"S-1-5-21-.*-513", sid) else "")
        if not name or "IO" in _pairs(flags) or not _grants_read(rights):   # IO: only for children
            continue
        if kind == "D":
            denied.add(name)
        elif name not in denied and name not in out:
            out.append(name)
    return out


def file_sddl(path: Path | str) -> str | None:  # pragma: no cover - Windows only: reads the file's ACL
    """The file's permissions (DACL) in SDDL form, or None if they can't be read."""
    import ctypes
    from ctypes import wintypes
    advapi, kernel = ctypes.WinDLL("advapi32"), ctypes.WinDLL("kernel32")
    get = advapi.GetNamedSecurityInfoW
    get.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    get.restype = wintypes.DWORD
    conv = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
    conv.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    conv.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    sd, text = ctypes.c_void_p(), ctypes.c_void_p()
    if get(str(path), 1, 4, None, None, None, None, ctypes.byref(sd)) != 0:   # 1: a file, 4: its DACL
        return None
    try:
        if not conv(sd, 1, 4, ctypes.byref(text), None):
            return None
        try:
            return ctypes.wstring_at(text.value)
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.LocalFree(sd)


def secrets_file_warning(path: Path | str, sddl: str | None, user: str) -> str | None:
    """The warning for a secrets file that other users can read (or whose permissions are unknown)."""
    if sddl is None:
        return f"{path}: its permissions could not be read, so check yourself that other users can't read it"
    who = broad_readers(sddl)
    if not who:
        return None
    # inheritance:r drops inherited entries only, so entries set on the file itself are removed by name
    remove = "".join(f' /remove "*{_SID[w]}"' for w in who if w in _SID)
    return (f"{path} is readable by other users ({', '.join(who)}). "
            f'Fix with: icacls "{path}" /inheritance:r /grant:r "{user}:F"{remove}')


# ------------------------------------------------------------------ W6: stopping a job
OWN_GROUP = "_gatewright_own_group"    # set on a Popen that leads its own process group


def request_stop(proc: subprocess.Popen) -> bool:
    """Windows: send Ctrl+Break to the job's process group, so tools that handle it can
    clean up. Returns whether it was sent.

    Only for processes Gatewright started in their own group: for any other pid, Windows
    may deliver the event to everyone on the console, Gatewright included.
    """
    event = getattr(signal, "CTRL_BREAK_EVENT", None)
    if event is None or not getattr(proc, OWN_GROUP, False) or proc.poll() is not None:
        return False
    try:
        os.kill(proc.pid, event)
    except (OSError, SystemError):      # no console (a service), or it just ended
        return False
    return True
