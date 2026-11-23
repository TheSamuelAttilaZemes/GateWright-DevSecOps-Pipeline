# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Local builds: paths, archives, isolation, the native dialogs, the CLI and the `o` window."""

import asyncio
import base64
import contextvars
import io
import os
import random
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from pathlib import Path

import pytest

from gatewright import cli, local, picker, runner, templates, tui, tui_open
from gatewright.config import ConfigError, load_pipeline
from gatewright.engine import Engine, Selection, git_info
from gatewright.local import LocalError
from gatewright.locks import file_lock
from gatewright.models import Job, plain
from gatewright.tui import GatewrightApp

from .conftest import BASIC_PIPELINE, PY, make_project, posix_only

OWN = "jobs:\n  - {id: hello, gate: commit, run: 'echo hello from the subject', cache: false}\n"


@pytest.fixture(autouse=True)
def state(tmp_path_factory, monkeypatch):
    d = tmp_path_factory.mktemp("state") / "local"
    monkeypatch.setattr(local, "state_dir", lambda: d)
    return d


@pytest.fixture
def subject(tmp_path):
    d = tmp_path / "My build (v1) [x]"
    d.mkdir()
    (d / "app.py").write_text("print('hi')\n")
    return d


def zip_of(path, files, **attr):
    with zipfile.ZipFile(path, "w") as z:
        for name, text in files.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = attr.get("mode", 0o644) << 16
            z.writestr(info, text)
    return path


def tar_of(path, members, mode="w:gz"):
    with tarfile.open(path, mode) as t:
        for m, data in members:
            if isinstance(m, str):
                m = tarfile.TarInfo(m)
                m.size = len(data)
            t.addfile(m, io.BytesIO(data) if data is not None else None)
    return path


# ---------------------------------------------------------------- paths
def test_state_dir_per_user(monkeypatch, tmp_path):
    monkeypatch.undo()
    if os.name == "nt":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    else:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert local.state_dir() == tmp_path / "gatewright" / "local"
        monkeypatch.delenv("XDG_STATE_HOME")
        assert local.state_dir() == Path.home() / ".local" / "state" / "gatewright" / "local"
        return
    assert local.state_dir() == tmp_path / "gatewright" / "local"


@pytest.mark.parametrize("text, wsl, want", [
    ('  "/a b/c"  ', False, "/a b/c"), ("'/x'", False, "/x"), ("file:///a%20b", False, "/a b"),
    ("file:///C:/x%20y", False, "C:/x y"), ("C:\\Users\\me\\x", True, "/mnt/c/Users/me/x"),
    ("D:/x", True, "/mnt/d/x"), ("C:\\x", False, "C:\\x"), ("/a\0b", False, "/ab"), ('"', False, '"')])
def test_clean_text(text, wsl, want):
    assert local.clean_text(text, wsl) == want


def test_clean_text_expands(monkeypatch):
    monkeypatch.setenv("GW_X", "/somewhere")
    assert local.clean_text("$GW_X/y") == "/somewhere/y"
    assert local.clean_text("~") == os.path.expanduser("~")


def test_resolve(subject, tmp_path, state):
    assert local.resolve(f'"{subject}"') == subject.resolve()
    assert local.resolve(str(subject / "app.py")).is_file()
    for bad, why in (("", "Enter the full path"), ("relative/x", "not a full path"),
                     (str(tmp_path / "gone"), "does not exist"), (str(Path.home()), "too broad"),
                     (Path.home().anchor, "too broad")):
        with pytest.raises(LocalError, match=why):
            local.resolve(bad)
    state.mkdir(parents=True)
    (state / "x").mkdir()
    for inside in (state, state / "x", state.parent):
        with pytest.raises(LocalError, match="own results"):
            local.resolve(str(inside))


def test_resolve_odd_files(subject, monkeypatch):
    monkeypatch.setattr(local.os, "access", lambda *a: False)
    with pytest.raises(LocalError, match="not allowed"):
        local.resolve(str(subject))
    monkeypatch.setattr(local.Path, "is_dir", lambda self: False)
    monkeypatch.setattr(local.Path, "is_file", lambda self: False)
    with pytest.raises(LocalError, match="neither"):
        local.resolve(str(subject))

    def loop(self, strict=False):
        raise RuntimeError("Symlink loop")
    monkeypatch.setattr(local.Path, "resolve", loop)
    with pytest.raises(LocalError, match="can't be opened"):
        local.resolve(str(subject))


@posix_only
def test_resolve_device_and_fifo(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    for p in ("/dev/null", str(tmp_path / "pipe")):
        with pytest.raises(LocalError, match="neither"):
            local.resolve(p)


def test_each_subject_has_its_own_store(tmp_path):
    a, b = tmp_path / "a" / "app", tmp_path / "b" / "app"
    assert local.store_for(a) == local.store_for(a) != local.store_for(b)
    assert local.store_for(a).name.startswith("app-")
    assert local.store_for(Path("/Ω")).name.startswith("build-")
    if os.name == "nt":
        assert local.store_for(Path(str(a).upper())) .name[-12:] == local.store_for(a).name[-12:]


# ---------------------------------------------------------------- archives
@pytest.mark.parametrize("name", ["../x", "a/../../x", "/etc/passwd", "\\x", "C:\\x", "C:x", "a/b:stream", "..\\..\\x",
                                  "con", "a/NUL.txt", "x./y", "x /y", "a\nb", "a/com1"])
def test_unsafe_member_names(tmp_path, name):
    z = tmp_path / "x.zip"
    zip_of(z, {"ok.txt": "1"})
    with pytest.raises(LocalError, match="unsafe path"):
        local._safe_name(name)
    t = tar_of(tmp_path / "x.tar.gz", [("ok.txt", b"1"), (name, b"evil")])
    before = {p for p in tmp_path.rglob("*")}
    with pytest.raises(LocalError, match="unsafe path"):
        local.prepare(t)
    assert {p for p in tmp_path.rglob("*")} == before           # nothing next to the target


@pytest.mark.parametrize("name", [".", "./", "", "a", "a/b.txt", "./a//b", "…/ü", "con.d"])
def test_safe_member_names(name):
    if name == "con.d":                                          # reserved with any extension
        with pytest.raises(LocalError):
            local._safe_name(name)
    else:
        local._safe_name(name)


def test_zip_links_and_single_top_folder(tmp_path):
    z = zip_of(tmp_path / "app-1.0.zip", {"app-1.0/": "", "app-1.0/run.sh": "#!/bin/sh\n"}, mode=0o100755)
    with zipfile.ZipFile(z, "a") as f:
        link = zipfile.ZipInfo("app-1.0/link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        f.writestr(link, "/etc/passwd")
    b = local.prepare(z)
    root = Path(b.pipeline.root)
    assert root.name == "app-1.0" and (root / "run.sh").exists() and not (root / "link").exists()
    assert "1 link(s)" in b.note and not b.trusted and b.mode == "template"
    if os.name == "posix":
        assert os.access(root / "run.sh", os.X_OK)


@pytest.mark.parametrize("ext, mode", [(".tar", "w"), (".tgz", "w:gz"), (".tar.bz2", "w:bz2"), (".tar.xz", "w:xz")])
def test_tar_formats_and_special_members(tmp_path, ext, mode):
    link = tarfile.TarInfo("l")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    hard = tarfile.TarInfo("h")
    hard.type, hard.linkname = tarfile.LNKTYPE, "../../x"
    dev = tarfile.TarInfo("d")
    dev.type = tarfile.CHRTYPE
    folder = tarfile.TarInfo("sub")
    folder.type = tarfile.DIRTYPE
    root = tarfile.TarInfo("./")
    root.type = tarfile.DIRTYPE
    t = tar_of(tmp_path / f"x{ext}", [(root, None), (folder, None), ("sub/a.py", b"1\n"), ("b.py", b"2\n"), (link, None),
                                      (hard, None), (dev, None)], mode)
    b = local.prepare(t)
    root = Path(b.pipeline.root)
    assert sorted(p.name for p in root.iterdir()) == ["b.py", "sub"] and "3 link(s)" in b.note


def test_single_file(tmp_path):
    (tmp_path / "tool.py").write_text("print(1)\n")
    b = local.prepare(tmp_path / "tool.py")
    assert [p.name for p in Path(b.pipeline.root).iterdir()] == ["tool.py"]


def test_bombs_are_stopped(tmp_path, monkeypatch, state):
    monkeypatch.setattr(local, "MAX_BYTES", 3 * 1024**2)
    z = tmp_path / "bomb.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as f:
        f.writestr("big", b"\0" * (5 * 1024**2))
    with pytest.raises(LocalError, match="more than 3 MB"):
        local.prepare(z)
    assert not list(state.rglob("big"))                          # cleaned up
    monkeypatch.setattr(local, "MAX_FILES", 3)
    with pytest.raises(LocalError, match="entries"):
        local.prepare(zip_of(tmp_path / "many.zip", {str(i): "" for i in range(5)}))


def test_a_lying_zip_header_does_not_help(tmp_path, monkeypatch):
    """The declared size is small, the real one isn't."""
    monkeypatch.setattr(local, "MAX_BYTES", 1024**2)
    z = tmp_path / "lie.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as f:
        f.writestr("big", b"\0" * (4 * 1024**2))
    data = bytearray(z.read_bytes())
    for sig, off in ((b"PK\x01\x02", 24), (b"PK\x03\x04", 22)):
        i = data.index(sig)
        data[i + off:i + off + 4] = (10).to_bytes(4, "little")
    z.write_bytes(bytes(data))
    with pytest.raises(LocalError):
        local.prepare(z)


def test_damaged_and_empty_archives(tmp_path, monkeypatch):
    (tmp_path / "x.zip").write_bytes(b"not a zip")
    (tmp_path / "x.tar.gz").write_bytes(b"\x1f\x8b" + b"junk" * 20)
    zip_of(tmp_path / "empty.zip", {})
    for f in ("x.zip", "x.tar.gz"):
        with pytest.raises(LocalError, match="could not be unpacked|damaged"):
            local.prepare(tmp_path / f)
    with pytest.raises(LocalError, match="no files"):
        local.prepare(tmp_path / "empty.zip")

    def broken(*a):
        raise zlib_error("bad data")
    import zlib
    zlib_error = zlib.error
    monkeypatch.setattr(local, "_unzip", broken)
    with pytest.raises(LocalError, match="damaged"):
        local.prepare(tmp_path / "empty.zip")


def test_fuzzed_archives_never_crash_or_escape(tmp_path, state):
    """Random damage: either it unpacks inside its own folder or it is refused, nothing else."""
    rnd = random.Random(20261123)
    zip_of(tmp_path / "ok.zip", {f"d/f{i}.py": "x = 1\n" * 20 for i in range(6)})
    tar_of(tmp_path / "ok.tar.gz", [(f"d/f{i}.py", b"x = 1\n" * 20) for i in range(6)])
    tar_of(tmp_path / "ok.tar", [(f"d/f{i}.py", b"x = 1\n" * 20) for i in range(6)], "w")
    good = {f: (tmp_path / f).read_bytes() for f in ("ok.zip", "ok.tar.gz", "ok.tar")}
    work = tmp_path / "work"
    work.mkdir()
    for n in range(240):
        name = rnd.choice(sorted(good))
        data = bytearray(good[name])
        for _ in range(rnd.randint(1, 12)):
            data[rnd.randrange(len(data))] = rnd.randrange(256)
        if rnd.random() < 0.3:
            del data[rnd.randrange(len(data)):]
        f = tmp_path / f"fuzz{Path(name).suffix if name != 'ok.tar.gz' else '.tar.gz'}"
        f.write_bytes(bytes(data))
        out = work / str(n)
        try:
            root, _ = local.unpack(f, out)
            assert out.resolve() == Path(str(root).removeprefix("\\\\?\\")).resolve() or \
                out.resolve() in Path(str(root).removeprefix("\\\\?\\")).resolve().parents
        except LocalError:
            pass
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([*good, "fuzz.zip", "fuzz.tar", "fuzz.tar.gz", "work"])


def test_fuzzed_member_names_stay_inside():
    rnd = random.Random(7)
    base = Path("/base/dir") if os.name == "posix" else Path("C:/base/dir")
    alphabet = ["..", ".", "/", "\\", "a", "b.txt", ":", " ", "C:", "con", "\0", "ü", "~", "$HOME", "//", "-"]
    for _ in range(5000):
        name = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 7)))
        try:
            rel = local._safe_name(name)
        except LocalError:
            continue
        if rel is not None:
            full = os.path.normpath(os.path.join(base, *rel.parts))
            assert full.startswith(os.path.normpath(base) + os.sep), name


def test_reopening_replaces_the_copy(tmp_path):
    z = zip_of(tmp_path / "a.zip", {"old.py": "1", "x.py": "1"})
    root = Path(local.prepare(z).pipeline.root)
    ro = root / "old.py"
    ro.chmod(stat.S_IREAD)                                       # e.g. git objects
    zip_of(z, {"new.py": "1", "x.py": "2"})
    root = Path(local.prepare(z).pipeline.root)
    assert sorted(p.name for p in root.iterdir()) == ["new.py", "x.py"]


def test_busy_and_stuck_copies(tmp_path, monkeypatch):
    z = zip_of(tmp_path / "a.zip", {"x.py": "1"})
    with file_lock(local.store_for(z) / "prepare.lock"):
        with pytest.raises(LocalError, match="another Gatewright"):
            local.prepare(z)
    local.prepare(z)

    def busy(*a, **k):
        raise PermissionError("in use")
    with monkeypatch.context() as m:
        m.setattr(local.os, "replace", busy)
        with pytest.raises(LocalError, match="still in use"):
            local.prepare(z)
    with monkeypatch.context() as m:
        m.setattr(shutil, "rmtree", busy)
        with pytest.raises(LocalError, match="could not be replaced"):
            local.prepare(z)


# ---------------------------------------------------------------- pipelines and isolation
def test_modes(subject, project):
    home = load_pipeline(project(BASIC_PIPELINE, OWN.replace("hello", "theirs")))
    with pytest.raises(LocalError, match="no pipeline of its own"):
        local.prepare(subject, "own")
    with pytest.raises(LocalError, match="no current project"):
        local.prepare(subject, "project")
    with pytest.raises(LocalError, match="Unknown pipeline choice"):
        local.prepare(subject, "x")
    b = local.prepare(subject)
    assert b.mode == "template" and b.pipeline.name == subject.name and b.pipeline.root == str(subject)
    assert not (subject / ".gatewright").exists()                # the subject itself is not changed
    b = local.prepare(subject, "project", home)
    assert [j.id for j in b.pipeline.jobs] == ["theirs"] and not b.trusted
    make_project(subject, BASIC_PIPELINE, OWN)
    b = local.prepare(subject)
    assert b.mode == "own" and not b.trusted and b.data_root != str(subject)      # auto never gives away secrets
    b = local.prepare(subject, "own")
    assert b.trusted and b.data_root == str(subject)


def test_project_settings_do_not_follow(subject, project):
    home = load_pipeline(project(BASIC_PIPELINE + """\
signing: {key: /k, identity: me}
environments: {prod: {deploy: 'echo deploying'}}
notify: {channels: [{type: ntfy, url: 'https://ntfy.sh/mine'}]}
vcs: {provider: github, repo: me/app}
keep_runs: 5
""", OWN))
    p = local.prepare(subject, "project", home).pipeline
    assert (p.vcs, p.signing, p.environments, p.notify) == ({}, {}, {}, {}) and p.keep_runs == 5


def test_bad_pipelines_are_reported(subject):
    make_project(subject, "gates: 5\n", OWN)
    with pytest.raises(ConfigError):
        local.prepare(subject)
    (subject / ".gatewright" / "pipeline.yml").write_text("- just\n- a list\n")
    with pytest.raises(ConfigError):
        local.prepare(subject)


def test_runs_are_separate(tmp_path, project):
    home = project(BASIC_PIPELINE, OWN)
    Engine(load_pipeline(home)).run(Selection(reports=["md"]))
    subjects = []
    for n in ("one", "two"):
        d = tmp_path / "subjects" / n
        make_project(d, BASIC_PIPELINE, OWN.replace("hello from", f"{n} says"))
        b = local.prepare(d)
        r = Engine(b.pipeline, data_root=b.data_root, trusted=b.trusted).run(Selection(reports=["md"]))
        assert r.status == "passed"
        subjects.append((b, r))
        assert not (d / ".gatewright" / "runs").exists()
    for b, r in subjects:
        from gatewright.history import History
        assert [x["id"] for x in History(b.data_root).list(10)] == [r.run_id]
    assert len(list((home / ".gatewright" / "runs").glob("*/"))) >= 1
    assert cli.main(["-C", subjects[0][0].data_root, "history"]) == 0


def test_results_folder_can_not_be_run(subject, capsys):
    b = local.prepare(subject)
    for cmd in (["run"], ["tui"], ["rerun-failed"]):
        with pytest.raises(SystemExit):
            cli.main(["-C", b.data_root, *cmd])
    assert "only holds the results" in " ".join(capsys.readouterr().err.split())


def test_untrusted_code_finds_no_secrets(subject, monkeypatch):
    """Regression: every job inherited GATEWRIGHT_SECRET_* from Gatewright's own environment."""
    for k in ("GATEWRIGHT_SECRET_DEPLOY", "VCS_TOKEN", "GH_TOKEN", "DEPLOY"):
        monkeypatch.setenv(k, "topsecret-" + k)
    show = "import os\nprint('FOUND', sorted(k for k, v in os.environ.items() if 'topsecret' in v))\n"
    make_project(subject, BASIC_PIPELINE, f"jobs:\n  - {{id: a, gate: commit, cache: false, run: '{PY} show.py'}}\n",
                 {"show.py": show})

    def log(mode):
        b = local.prepare(subject, mode)
        r = Engine(b.pipeline, data_root=b.data_root, trusted=b.trusted).run(Selection(reports=["md"]))
        return (Path(r.artifact_dir) / "logs" / "a.log").read_text(encoding="utf-8")
    assert "FOUND ['DEPLOY']" in log("auto")                     # not one Gatewright knows about: L1
    assert "FOUND ['DEPLOY', 'GH_TOKEN', 'VCS_TOKEN']" in log("own")
    env = runner.job_environ(Job(id="a", gate="g", run="x", secrets=["deploy"]), False)
    assert "DEPLOY" not in env


def test_untrusted_code_can_not_hide_its_findings(subject):
    """Regression: a pull request's own baseline.yml was used to suppress its findings."""
    scan = "import json\nprint(json.dumps([dict(RuleID='r', Description='d', File='a.py', StartLine=1)]))\nraise SystemExit(1)\n"
    make_project(subject, BASIC_PIPELINE,
                 f"jobs:\n  - {{id: a, gate: commit, cache: false, run: '{PY} scan.py', parser: gitleaks}}\n",
                 {"scan.py": scan, "a.py": "x\n",
                  ".gatewright/baseline.yml": "suppressions:\n  - {rule: r, reason: nothing to see}\n"})
    for mode, want in (("auto", "failed"), ("own", "passed")):
        b = local.prepare(subject, mode)
        assert Engine(b.pipeline, data_root=b.data_root, trusted=b.trusted).run(Selection(reports=["md"])).status == want


@pytest.mark.parametrize("name", ["yes", "a: b", "#x", "Ünï & Co", "{gates}", "it's", "- x", "null", "..x"])
def test_any_folder_name_makes_a_valid_template(tmp_path, name):
    """Regression: the folder name was pasted into YAML and into docker image tags unquoted."""
    for t in templates.TEMPLATES:
        d = tmp_path / t
        templates.write(d, t, name)
        p = load_pipeline(d)
        assert p.name == name
        for j in p.jobs:
            if "gatewright-local/" in j.run:
                tag = j.run.split("gatewright-local/")[1].split(":")[0]
                assert tag and tag == tag.lower() and all(c.isalnum() and c.isascii() or c == "-" for c in tag)


def test_repository_without_commits(tmp_path):
    """Regression: the commit was reported as "HEAD"."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert git_info(str(tmp_path)) == ("", "", False)


def test_dirty_means_this_folder(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
        (d / "f").write_text("1")
    for c in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"]):
        subprocess.run(["git", *c], cwd=tmp_path, check=True)
    (b / "f").write_text("2")
    assert git_info(str(a))[2] is False and git_info(str(b))[2] is True


# ---------------------------------------------------------------- native dialogs
def test_dialog_commands_per_platform(monkeypatch):
    def has(*names):
        return lambda n: f"/bin/{n}" if n in names else None

    def names(*a, **k):
        return [Path(c[0]).name for c in picker.commands(*a, **k)]
    assert names("folder", "win32", {}, has("powershell"), has_tk=False) == ["powershell"]
    assert names("folder", "win32", {}, has("pwsh"), has_tk=True) == [Path(sys.executable).name, "pwsh"]
    assert names("folder", "win32", {}, has(), has_tk=False) == []
    assert names("file", "darwin", {}, has("osascript")) == ["osascript"] and names("file", "darwin", {}, has()) == []
    assert names("file", "linux", {}, has("zenity", "kdialog"), has_tk=True) == []           # SSH: no display
    assert names("file", "linux", {"DISPLAY": ":0"}, has("zenity", "yad", "kdialog"), has_tk=False) == \
        ["zenity", "yad", "kdialog"]
    assert names("folder", "linux", {}, has("powershell.exe"), wsl=True, has_tk=False) == ["powershell.exe"]
    assert names("file", "linux", {"DISPLAY": ":0"}, has("zenity"), has_tk=False) == ["zenity"]      # no KDE
    assert names("file", "linux", {"DISPLAY": ":0"}, has(), has_tk=True) == [Path(sys.executable).name]
    dirs = picker.commands("folder", "linux", {"WAYLAND_DISPLAY": "w"}, has("zenity", "kdialog"), has_tk=False)
    assert "--directory" in dirs[0] and "--getexistingdirectory" in dirs[1]
    files = picker.commands("file", "linux", {"WAYLAND_DISPLAY": "w"}, has("zenity", "kdialog"), has_tk=False)
    assert "--directory" not in files[0] and "--getopenfilename" in files[1]
    script = base64.b64decode(picker.powershell_args("file")[-1]).decode("utf-16-le")
    assert "OpenFileDialog" in script and "FolderBrowserDialog" in base64.b64decode(
        picker.powershell_args("folder")[-1]).decode("utf-16-le")
    assert isinstance(picker.commands("file"), list)


def py(code):
    return [sys.executable, "-c", code]


@pytest.mark.parametrize("cmds, path, problem", [
    ([py("print('/a b/ü')")], "/a b/ü", ""),
    ([py("pass")], "", ""),                                                   # cancelled (exit 0)
    ([py("import sys; sys.stderr.write('dbind-WARNING'); sys.exit(252)")], "", ""),      # cancelled (yad)
    ([py("raise SystemExit(1)")], "", ""),                                    # cancelled (zenity)
    ([py("import sys; sys.stderr.write('Gtk-WARNING: theme'); sys.exit(1)")], "", ""),
    ([py("import sys; sys.stderr.write('User canceled. (-128)'); sys.exit(2)")], "", ""),
    ([py("import sys; sys.stderr.write('cannot open display: :9'); sys.exit(1)")], "", "cannot open display"),
    ([py("raise SystemExit(3)")], "", "exit code 3"),
    ([py("import sys; sys.stderr.write('cannot open display'); sys.exit(1)"), py("print('/b')")], "/b", ""),
    ([["/does/not/exist"]], "", "could not be started"),
    ([], "", "No graphical file dialog")])
def test_picker_outcomes(cmds, path, problem, monkeypatch):
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    got, why = picker.Picker(cmds).choose()
    assert got == path and problem in why and bool(why) == bool(problem)


def test_picker_can_be_cancelled():
    p = picker.Picker([py("import time; time.sleep(60)")])
    out = []
    t = threading.Thread(target=lambda: out.append(p.choose()))
    t.start()
    while p.proc is None:
        time.sleep(0.05)
    p.cancel()
    t.join(timeout=20)
    assert out == [("", "")]
    assert p.choose() == ("", "")                                # stays cancelled

    class Late(picker.Picker):                                   # cancelled while the dialog was starting
        def _read(self, proc):
            self.cancelled = True
            return super()._read(proc)
    assert Late([py("import time; time.sleep(60)")]).choose() == ("", "")


def test_picker_ignores_a_lot_of_error_output():
    """Regression: a toolkit that prints more errors than fit in a pipe hung the dialog."""
    assert picker.Picker([py("import sys; sys.stderr.write('x' * 500000); print('/a')")]).choose() == ("/a", "")


@pytest.mark.parametrize("early", [True, False])
@pytest.mark.parametrize("taskkill", ["ok", "missing"])
def test_wsl_dialog_is_closed_on_the_windows_side(monkeypatch, early, taskkill):
    """Regression: killing the relay left Explorer's dialog open."""
    exe = [sys.executable if sys.executable.endswith(".exe") else "fake.exe"]
    real, killed = subprocess.Popen, []
    code = f"import time; time.sleep(1); print('{picker.PID_MARK}4711', flush=True); time.sleep(60)"

    def popen(cmd, **kw):
        proc = real(py(code), **kw)
        proc.args = exe
        return proc

    def run(cmd, **kw):
        killed.append(cmd)
        if taskkill == "missing":
            raise OSError("not found")
    monkeypatch.setattr(picker.subprocess, "Popen", popen)
    monkeypatch.setattr(picker.subprocess, "run", run)
    p = picker.Picker([exe], wsl=True)
    out = []
    t = threading.Thread(target=lambda: out.append(p.choose()))
    t.start()
    while p.proc is None or not (early or p.remote_pid):
        time.sleep(0.02)
    p.cancel()
    t.join(timeout=30)
    assert out == [("", "")] and killed == [["taskkill.exe", "/F", "/PID", "4711"]]


# ---------------------------------------------------------------- command line
def test_cli_run_local(subject, project, capsys, monkeypatch):
    home = project(BASIC_PIPELINE, OWN)
    make_project(subject, BASIC_PIPELINE, OWN)
    assert cli.main(["-C", str(home), "run", "--local", str(subject)]) == 0
    out = capsys.readouterr().out
    assert "Testing" in out and "[x]" in out and "gatewright -C" in out
    assert not (subject / ".gatewright" / "runs").exists()
    monkeypatch.chdir(subject)
    assert cli.main(["-C", str(home), "run", "--local", ".", "--pipeline", "project", "--dry-run"]) == 0
    assert cli.main(["plan", "--local", "app.py"]) == 0
    for args, code in ((["--local", str(subject / "gone")], 2), (["--local", "rel"], 2), (["--local", ""], 2)):
        with pytest.raises(SystemExit) as e:
            cli.main(["run", *args])
        assert e.value.code == code
    (subject / ".gatewright" / "pipeline.yml").write_text("gates: 5")
    with pytest.raises(SystemExit):
        cli.main(["run", "--local", str(subject)])
    assert cli.shown("/a") == "/a" and cli.shown("/a b") == '"/a b"'


def test_cli_tui(subject, project, monkeypatch, tmp_path_factory):
    made = []

    class App:
        def __init__(self, *a, **k):
            made.append((a, k))

        def run(self):
            pass
    monkeypatch.setattr(tui, "GatewrightApp", App)
    home = project(BASIC_PIPELINE, OWN)
    assert cli.main(["-C", str(home), "tui"]) == 0 and made[-1][1]["pick"] is False
    assert cli.main(["-C", str(home), "tui", "--local"]) == 0 and made[-1][1]["pick"] is True
    assert cli.main(["-C", str(home), "tui", "--local", str(subject)]) == 0
    assert made[-1][1]["trusted"] is False and made[-1][1]["project"].name == "t"
    nowhere = tmp_path_factory.mktemp("nowhere")
    assert cli.main(["-C", str(nowhere), "tui"]) == 0
    assert made[-1][0][0] is None and made[-1][1]["pick"] is True


# ---------------------------------------------------------------- the `o` window
def drive(app, steps):
    async def go():
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause(0.3)
            return await steps(pilot)
    return asyncio.run(go())


async def until(pilot, cond, seconds=20):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        await pilot.pause(0.1)
        if cond():
            return True
    return False


def message(app):
    return str(app.screen.query_one("#open-message").render())


def test_tui_open_run_and_go_back(subject, project):
    home = project(BASIC_PIPELINE, OWN)
    make_project(subject, BASIC_PIPELINE, OWN.replace("hello", "subjectjob"))
    app = GatewrightApp(load_pipeline(home))

    async def steps(pilot):
        await pilot.press("o")
        await pilot.pause(0.2)
        assert isinstance(app.screen, tui_open.OpenBuild)
        app.screen.query_one("#open-path").value = f'"{subject}"'
        await pilot.press("enter")
        assert await until(pilot, lambda: not isinstance(app.screen, tui_open.OpenBuild))
        assert [j.id for j in app.p.jobs] == ["subjectjob"] and not app.engine.trusted
        await pilot.press("r")
        assert await until(pilot, lambda: app.last_run is not None and not app.running)
        status = app.last_run.status
        await pilot.press("o")                                   # again, and back to the project
        await pilot.pause(0.2)
        await pilot.click("#open-back")
        assert await until(pilot, lambda: not isinstance(app.screen, tui_open.OpenBuild))
        assert [j.id for j in app.p.jobs] == ["hello"] and app.last_run is None and app.engine.trusted
        await pilot.press("o")
        await pilot.pause(0.2)
        assert not app.screen.query("#open-back")                # already there
        await pilot.press("escape")
        await pilot.pause(0.2)
        return status
    assert drive(app, steps) == "passed"
    assert not (subject / ".gatewright" / "gatewright.db").exists()


def test_tui_open_refusals(subject, project, monkeypatch):
    app = GatewrightApp(load_pipeline(project(BASIC_PIPELINE, OWN)))

    async def steps(pilot):
        await pilot.press("o")
        await pilot.pause(0.2)
        scr = app.screen
        for text, why in (("", "Enter the full path"), ("nope", "not a full path"), (str(subject / "[gone"), "does not exist")):
            scr.query_one("#open-path").value = text
            await pilot.press("enter")
            assert await until(pilot, lambda w=why: w in message(app)), message(app)
        monkeypatch.setattr(local, "prepare", lambda *a: 1 / 0)
        scr.query_one("#open-path").value = str(subject)
        await pilot.click("#open-ok")
        assert await until(pilot, lambda: "can't be opened" in message(app))
        scr.busy = True
        scr._open()
        scr.action_browse("file")                                # busy: ignored
        scr.busy = False
        await pilot.click("#open-cancel")
        await pilot.pause(0.2)
        assert app.p.name == "t"
        app.running = True
        await pilot.press("o")
        await pilot.pause(0.2)
        app.running = False
        return isinstance(app.screen, tui_open.OpenBuild)
    assert drive(app, steps) is False


def test_tui_browse(subject, project, monkeypatch):
    app = GatewrightApp(load_pipeline(project(BASIC_PIPELINE, OWN)))
    asked = []

    def cmds(kind, wsl=False):
        asked.append(kind)
        return [py(f"import sys; sys.stdout.reconfigure(encoding='utf-8'); print({str(subject)!r})")] if kind == "folder" else []
    monkeypatch.setattr(picker, "commands", cmds)

    async def steps(pilot):
        await pilot.press("o")
        await pilot.pause(0.2)
        await pilot.click("#open-folder")
        assert await until(pilot, lambda: app.screen.query_one("#open-path").value == str(subject))
        await pilot.press("ctrl+f")
        assert await until(pilot, lambda: "No graphical file dialog" in message(app))
        tree = app.screen.query_one("#open-tree")
        tree.post_message(tree.FileSelected(tree.root, subject / "app.py"))
        assert await until(pilot, lambda: app.screen.query_one("#open-path").value.endswith("app.py"))
        tree.post_message(tree.DirectorySelected(tree.root, subject))
        assert await until(pilot, lambda: app.screen.query_one("#open-path").value == str(subject))
        await pilot.press("escape")
    drive(app, steps)
    assert asked == ["folder", "file"]


def test_tui_closing_the_window_closes_the_dialog(project, monkeypatch):
    app = GatewrightApp(load_pipeline(project(BASIC_PIPELINE, OWN)))
    monkeypatch.setattr(picker, "commands", lambda kind, wsl=False: [py("import time; time.sleep(60)")])

    async def steps(pilot):
        await pilot.press("o")
        await pilot.pause(0.2)
        await pilot.press("ctrl+o")
        scr = app.screen
        assert await until(pilot, lambda: scr.picker.proc is not None)
        await pilot.press("escape")
        assert await until(pilot, lambda: scr.picker.proc.poll() is not None)
    drive(app, steps)


def test_tui_outside_a_project(subject):
    app = GatewrightApp(None, pick=True)

    async def steps(pilot):
        assert isinstance(app.screen, tui_open.OpenBuild)        # asks at once
        assert app.screen.query("RadioButton")[2].disabled       # no current project
        await pilot.press("escape")
        await pilot.pause(0.2)
        for key in ("r", "d", "f", "u", "a"):                    # nothing open: no crash
            await pilot.press(key)
            await pilot.pause(0.1)
        await pilot.press("o")
        await pilot.pause(0.2)
        app.screen.query_one("#open-path").value = str(subject)
        app.screen.query("RadioButton")[3].value = True
        await pilot.press("enter")
        assert await until(pilot, lambda: not isinstance(app.screen, tui_open.OpenBuild))
        return app.p.name
    assert drive(app, steps) == subject.name


def test_tui_unwritable_subject(subject, project, monkeypatch):
    app = GatewrightApp(load_pipeline(project(BASIC_PIPELINE, OWN)))
    build = local.prepare(subject)

    async def steps(pilot):
        real = tui.History

        def history(root):
            if root == build.data_root:
                raise PermissionError("read only")
            return real(root)
        monkeypatch.setattr(tui, "History", history)
        app._opened(build)
        await pilot.pause(0.2)
        return app.p.name
    assert drive(app, steps) == "t"


def test_late_answers_after_closing_are_ignored(project):
    app = GatewrightApp(load_pipeline(project(BASIC_PIPELINE, OWN)))

    async def steps(pilot):
        await pilot.press("o")
        await pilot.pause(0.2)
        scr = app.screen
        await pilot.press("escape")
        await pilot.pause(0.2)
        hit = []
        ctx = contextvars.copy_context()                          # as Textual's thread workers have
        t = threading.Thread(target=lambda: ctx.run(scr._from_thread, hit.append, 1))
        t.start()
        await until(pilot, lambda: not t.is_alive())
        scr._from_thread(hit.append, 2)                          # wrong thread / stopped app: no exception
        return hit
    assert drive(app, steps) == []


# ---------------------------------------------------------------- hostile output
@pytest.mark.parametrize("text, want", [
    ("\x1b]52;c;ZXZpbA==\x07x", "x"), ("\x1b]0;title\x1b\\x", "x"), ("\x1b[2J\x1b[31mred\x1b[0m", "red"),
    ("a\x9b31mb", "a31mb"), ("\x1bPq#0\x1b\\x", "q#0x"), ("a\rb\x08c\x00", "abc"), ("tab\tline\nü✓", "tab\tline\nü✓"),
    ("\x1b]unterminated", ""), ("\x1b", "")])
def test_plain(text, want):
    assert plain(text) == want


def test_output_can_not_control_the_terminal(subject, monkeypatch):
    """Regression: escape sequences printed by tested code reached the terminal."""
    monkeypatch.setenv("GATEWRIGHT_SECRET_T", "TOPSECRET")
    evil = ("import os\nt = os.environ['T']\nprint('\\x1b]52;c;ZXZpbA==\\x07\\x1b[2Jlast ' + t[:3] + '\\x1b[0m' + t[3:])\n"
            "raise SystemExit(1)\n")
    make_project(subject, BASIC_PIPELINE,
                 f'jobs:\n  - {{id: a, gate: commit, cache: false, secrets: [T], name: "n\\e[2J", run: \'{PY} evil.py\'}}\n',
                 {"evil.py": evil})
    b = local.prepare(subject, "own")
    lines = []
    r = Engine(b.pipeline, lambda k, d: lines.append(d["line"]) if k == "job_log" else None).run(Selection(reports=["md"]))
    log = (Path(r.artifact_dir) / "logs" / "a.log").read_text(encoding="utf-8")
    texts = [log, *lines, b.pipeline.jobs[0].name, *(e.human + e.raw for e in r.all_jobs()[0].errors)]
    for t in texts:
        assert "\x1b" not in t and "\x07" not in t and "TOPSECRET" not in t
    assert "last ***" in log


@pytest.mark.parametrize("parser, text", [("sarif", "garbage{"), ("sarif", None), ("gitleaks", "{"), ("gitleaks", None),
                                          ("junit", "<x"), ("junit", None), ("coverage", "")])
def test_unreadable_results_never_pass(subject, parser, text):
    """Regression: a tool that exits 0 with a missing or damaged report passed its job."""
    write = "" if text is None else f"import os\nopen(os.environ['GATEWRIGHT_OUT'] + '/r', 'w').write({text!r})\n"
    make_project(subject, BASIC_PIPELINE, f"""\
        jobs:
          - {{id: a, gate: commit, cache: false, run: '{PY} w.py', parser: {{type: {parser}, file: $GATEWRIGHT_OUT/r}}}}
    """, {"w.py": write or "pass\n"})
    b = local.prepare(subject)
    r = Engine(b.pipeline, data_root=b.data_root, trusted=False).run(Selection(reports=["md"]))
    assert r.status == "failed" and r.all_jobs()[0].status == "error"
    assert "could not read its results" in r.all_jobs()[0].errors[0].human


def test_damaged_output_without_a_report_file(subject):
    from gatewright.errors import parse_output
    job = Job(id="a", gate="g", run="x", parser={"type": "sarif"})
    assert parse_output(job, "", "", 0, str(subject), None) == ([], {}, None)      # nothing printed, nothing found
    assert parse_output(job, "{broken", "", 0, str(subject), None)[2].category == "crash"


def test_xml_bombs_and_entities(subject):
    from gatewright.errors import parse_output
    job = Job(id="a", gate="g", run="x", parser={"type": "junit", "file": "r"})
    bomb = '<!DOCTYPE l [<!ENTITY a0 "aaaaaaaaaaaaaaaaaaaa">' + "".join(
        f'<!ENTITY a{i} "{f"&a{i - 1};" * 20}">' for i in range(1, 9)) + ']><testsuite><testcase name="&a8;"/></testsuite>'
    t = time.monotonic()
    assert parse_output(job, "", "", 0, str(subject), bomb)[2] is not None and time.monotonic() - t < 10
    (subject / "s").write_text("TOPSECRET")
    xxe = f'<!DOCTYPE l [<!ENTITY e SYSTEM "{(subject / "s").as_uri()}">]><testsuite><testcase><failure>&e;</failure></testcase></testsuite>'
    assert "TOPSECRET" not in str(parse_output(job, "", "", 1, str(subject), xxe))
