# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The desktop's own "choose a folder / file" window.

Windows  Explorer's dialog (tkinter if Python has it, else PowerShell)
WSL      Explorer's dialog of the Windows side
Linux    zenity, kdialog, yad or tkinter, when there is a graphical session
macOS    Finder's dialog

The dialog runs in its own process, so a crash or a hang in it can't take the terminal UI
down, and closing the picker stops it. Without any of these (SSH, a server), `commands()` is empty and
the terminal UI's folder tree and path field are used.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess  # nosec B404 - fixed commands, no user input in them
import sys
import tempfile
from typing import Callable

TITLE = "Gatewright: choose what to test"
PID_MARK = "gatewright-dialog-pid:"

_TK = """
import sys, tkinter
from tkinter import filedialog
sys.stdout.reconfigure(encoding="utf-8")
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass
root = tkinter.Tk()
root.withdraw()
root.attributes("-topmost", True)
pick = filedialog.askdirectory if sys.argv[1] == "folder" else filedialog.askopenfilename
extra = {"mustexist": True} if sys.argv[1] == "folder" else {}
print(pick(parent=root, title=sys.argv[2], **extra) or "")
"""

_PS = {
    "folder": "$d=New-Object System.Windows.Forms.FolderBrowserDialog;$d.Description=$t;$d.ShowNewFolderButton=$false;"
              "if($d.ShowDialog($o) -eq 'OK'){$d.SelectedPath}",
    "file": "$d=New-Object System.Windows.Forms.OpenFileDialog;$d.Title=$t;"
            "if($d.ShowDialog($o) -eq 'OK'){$d.FileName}",
}


def powershell_args(kind: str) -> list[str]:
    """Arguments for powershell.exe. Encoded, so no shell in between can change the script."""
    script = (f"[Console]::OutputEncoding=[Text.Encoding]::UTF8;[Console]::WriteLine('{PID_MARK}'+$PID);Add-Type -AssemblyName System.Windows.Forms;"
              f"$t='{TITLE}';$o=New-Object System.Windows.Forms.Form -Property @{{TopMost=$true}};" + _PS[kind])
    return ["-NoProfile", "-STA", "-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode()]


def _has_tk() -> bool:
    import importlib.util
    return importlib.util.find_spec("tkinter") is not None and importlib.util.find_spec("_tkinter") is not None


def commands(kind: str, platform: str = sys.platform, env=None, which: Callable = shutil.which, wsl: bool = False,
             has_tk: bool | None = None) -> list[list[str]]:
    """The dialog commands that could work here, best first. `kind`: folder | file."""
    env = os.environ if env is None else env
    has_tk = _has_tk() if has_tk is None else has_tk
    tk = [[sys.executable, "-I", "-c", _TK, kind, TITLE]] if has_tk else []
    if platform == "win32":
        ps = which("powershell") or which("pwsh")
        return tk + ([[ps, *powershell_args(kind)]] if ps else [])
    if platform == "darwin":
        osa = which("osascript")
        return [[osa, "-e", f'POSIX path of (choose {kind} with prompt "{TITLE}")']] if osa else []
    out: list[list[str]] = []
    if wsl and which("powershell.exe"):
        out.append([which("powershell.exe"), *powershell_args(kind)])
    if env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"):
        folder = kind == "folder"
        for tool in ("zenity", "yad"):
            if which(tool):
                out.append([which(tool), "--file-selection", f"--title={TITLE}", *(["--directory"] if folder else [])])
        if which("kdialog"):
            out.append([which("kdialog"), "--title", TITLE,
                        "--getexistingdirectory" if folder else "--getopenfilename", os.path.expanduser("~")])
        out += tk
    return out


class Picker:
    """Show the dialog: `choose()` blocks (call it in a thread), `cancel()` closes the dialog."""

    def __init__(self, cmds: list[list[str]], wsl: bool = False):
        self.cmds, self.wsl = cmds, wsl
        self.proc: subprocess.Popen | None = None
        self.remote_pid = ""           # WSL: the dialog is a Windows process, out of reach of kill()
        self.cancelled = False

    def choose(self) -> tuple[str, str]:
        """(path, problem). Both empty: the user cancelled."""
        problem = "No graphical file dialog is available here. Type the path, or use the folder list."
        for cmd in self.cmds:
            if self.cancelled:
                return "", ""
            # errors go to a file: a chatty toolkit must not block on a full pipe
            with tempfile.TemporaryFile() as err:
                try:
                    self.remote_pid = ""
                    self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,  # nosec B603
                                                 stderr=err, creationflags=_NO_WINDOW)
                except OSError as e:
                    problem = f"The file dialog could not be started: {e}"
                    continue
                path = self._read(self.proc)
                err.seek(0)
                text = err.read().decode("utf-8", "replace").strip()
            if path or self.cancelled:
                return path if not self.cancelled else "", ""
            # cancelling: exit code 0 without a path (tk, Windows), 1 (zenity, kdialog), 252 (yad), -128 (Finder)
            code = self.proc.returncode
            if code in (0, 252) or (code == 1 and not _failed(text)) or "-128" in text:
                return "", ""
            problem = "The file dialog failed: " + (text.splitlines()[-1][:200] if text else f"exit code {code}")
        return "", problem

    def _read(self, proc: subprocess.Popen) -> str:
        path = ""
        if self.cancelled:      # cancel() came while it was starting
            self._stop(proc)
        for raw in proc.stdout or []:
            line = raw.decode("utf-8", "replace").strip("\r\n")
            if line.startswith(PID_MARK):
                self.remote_pid = line[len(PID_MARK):]
                if self.cancelled:
                    self._stop(proc)
            else:
                path = line or path
        proc.wait()
        return path

    def cancel(self) -> None:
        self.cancelled = True
        if self.proc is not None and self.proc.poll() is None:
            self._stop(self.proc)

    def _stop(self, proc: subprocess.Popen) -> None:
        if not (self.wsl and proc.args[0].endswith(".exe")):
            proc.kill()
        elif self.remote_pid.isdigit():     # not known yet: _read() stops it as soon as it is
            try:
                subprocess.run(["taskkill.exe", "/F", "/PID", self.remote_pid], capture_output=True,  # nosec B603 B607
                               timeout=15, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
            proc.kill()


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)     # Windows: no console window behind the dialog


def _failed(stderr: str) -> bool:
    """Exit code 1 means both "cancelled" and "broken": tell them apart by what was printed.
    GTK prints harmless warnings while working."""
    low = stderr.lower()
    return any(w in low for w in ("cannot open display", "can't open display", "couldn't connect to display",
                                  "no display name", "could not connect to display", "traceback", "unable to init"))
