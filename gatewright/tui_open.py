# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The `o` window of the terminal UI: choose a local folder, archive or file to test (see local.py)."""

from __future__ import annotations

import sys
from pathlib import Path

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, DirectoryTree, Input, Label, RadioButton, RadioSet, Static

from . import local, picker
from .config import ConfigError
from .models import Pipeline
from .platform_info import detect as detect_os

CHOICES = [("auto", "Automatic: its own jobs, else a fitting template (no secrets)"),
           ("own", "As a project of its own, with its secrets and settings (trusted folders only)"),
           ("project", "The jobs of the current project (no secrets)"),
           ("template", "The template that fits its files (no secrets)")]


class OpenBuild(ModalScreen):
    """Closes with a LocalBuild, "project" (back to the project) or None (cancelled)."""
    CSS = """
    OpenBuild { align: center middle; }
    #open-box { width: 90%; max-width: 110; height: 90%; border: thick $primary; background: $surface; padding: 0 1; }
    #open-tree { height: 1fr; border: round $primary-background; }
    #open-mode { height: auto; width: 100%; }
    #open-message { height: auto; min-height: 1; color: $warning; }
    .open-row { height: auto; }
    .open-row Button { margin-right: 1; }
    """
    BINDINGS = [Binding("escape", "close", "Cancel"), Binding("ctrl+o", "browse('folder')", "Browse folder"),
                Binding("ctrl+f", "browse('file')", "Browse file")]

    def __init__(self, project: Pipeline | None, start: Path, can_go_back: bool):
        super().__init__()
        self.project, self.start, self.can_go_back = project, start, can_go_back
        self.picker: picker.Picker | None = None
        self.busy = False

    def compose(self) -> ComposeResult:
        with Vertical(id="open-box"):
            yield Label("Test a build on this computer: folder, archive (.zip .whl .tar.*) or file")
            yield Input(placeholder="Full path", id="open-path")
            with Horizontal(classes="open-row"):
                yield Button("Browse folder (ctrl+o)", id="open-folder")
                yield Button("Browse file (ctrl+f)", id="open-file")
            yield DirectoryTree(str(self.start), id="open-tree")
            with RadioSet(id="open-mode"):
                for mode, text in CHOICES:
                    yield RadioButton(text, value=mode == "auto", disabled=mode == "project" and self.project is None)
            yield Static(id="open-message", markup=False)
            with Horizontal(classes="open-row"):
                yield Button("Open", id="open-ok", variant="primary")
                if self.can_go_back:
                    yield Button("Back to the project", id="open-back")
                yield Button("Cancel (esc)", id="open-cancel")

    def on_mount(self) -> None:
        self.query_one("#open-path", Input).focus()

    def on_unmount(self) -> None:
        if self.picker:
            self.picker.cancel()

    def _say(self, text: str) -> None:
        self.query_one("#open-message", Static).update(text)

    # ------------------------------------------------------------ choosing
    @on(DirectoryTree.DirectorySelected)
    @on(DirectoryTree.FileSelected)
    def _tree(self, ev) -> None:
        self.query_one("#open-path", Input).value = str(ev.path)

    def action_browse(self, kind: str) -> None:
        """ctrl+o / ctrl+f: the desktop's own dialog."""
        if self.busy:
            return
        wsl = sys.platform.startswith("linux") and detect_os().wsl
        self.picker = picker.Picker(picker.commands(kind, wsl=wsl), wsl)
        self.busy = True
        self._say("Choose in the window that opens (it can be behind this one)...")
        self._browse(self.picker)

    @work(thread=True)
    def _browse(self, p: picker.Picker) -> None:
        path, problem = p.choose()
        self._from_thread(self._browsed, path, problem)

    def _browsed(self, path: str, problem: str) -> None:
        self.busy = False
        self._say(problem)
        if path:
            self.query_one("#open-path", Input).value = path

    # ------------------------------------------------------------ opening
    @on(Button.Pressed)
    def _button(self, ev: Button.Pressed) -> None:
        what = (ev.button.id or "").removeprefix("open-")
        if what in ("folder", "file"):
            self.action_browse(what)
        elif what == "ok":
            self._open()
        elif what == "back":
            self.dismiss("project")
        else:
            self.action_close()

    @on(Input.Submitted)
    def _submitted(self) -> None:
        self._open()

    def action_close(self) -> None:
        self.dismiss(None)

    def _open(self) -> None:
        if self.busy:
            return
        index = self.query_one("#open-mode", RadioSet).pressed_index
        self.busy = True
        self._say("Preparing...")
        self._prepare(self.query_one("#open-path", Input).value, CHOICES[max(0, index)][0])

    @work(thread=True)
    def _prepare(self, text: str, mode: str) -> None:
        try:
            build = local.prepare(local.resolve(text), mode, self.project)
        except (local.LocalError, ConfigError, OSError) as e:
            self._from_thread(self._refused, str(e))
            return
        except Exception as e:  # a damaged pipeline file, ...: show it, don't close the UI
            self._from_thread(self._refused, f"It can't be opened: {e}")
            return
        self._from_thread(self._prepared, build)

    def _refused(self, why: str) -> None:
        self.busy = False
        self._say(why)

    def _prepared(self, build: local.LocalBuild) -> None:
        self.dismiss(build)

    def _from_thread(self, fn, *args) -> None:
        """Back to the UI thread, unless this window was closed in the meantime."""
        def guarded():
            if self.is_attached and self.is_current:
                fn(*args)
        try:
            self.app.call_from_thread(guarded)
        except (RuntimeError, NoMatches):
            pass
