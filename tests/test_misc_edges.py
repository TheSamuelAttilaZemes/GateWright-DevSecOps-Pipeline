# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Config validation, scheduler admission, templates, terminal rendering and report branches."""

import threading
import time

import pytest
from rich.console import Console

from gatewright import reports
from gatewright.config import ConfigError, load_pipeline
from gatewright.hardware import Budget
from gatewright.models import (BLOCKED, CANCELLED, FAILED, PASSED, SKIPPED, GateResult, Job, JobError, JobResult,
                               RunResult)
from gatewright.render import error_panel, summary_table
from gatewright.scheduler import Scheduler
from gatewright.templates import detect_template, init

from .conftest import BASIC_PIPELINE


# ---------------------------------------------------------------- config
def _problems(project, pipeline, jobs="jobs: []\n", extra_files=None):
    root = project(pipeline, jobs, files=extra_files)
    with pytest.raises(ConfigError) as e:
        load_pipeline(root)
    return "\n".join(e.value.problems)


def test_config_file_problems(project, tmp_path):
    root = project(BASIC_PIPELINE, "jobs: []\n")
    (root / ".gatewright" / "jobs" / "latin1.yml").write_bytes(b"jobs:\n  - {id: caf\xe9}\n")
    with pytest.raises(ConfigError, match="is not UTF-8 text"):        # regression: UnicodeDecodeError
        load_pipeline(root)


def test_config_unreadable_job_file(project, monkeypatch):
    root = project(BASIC_PIPELINE, "jobs: []\n")
    from pathlib import Path
    real = Path.read_text

    def deny(self, *a, **k):
        if self.name == "jobs.yml":
            raise PermissionError("denied")
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", deny)
    with pytest.raises(ConfigError, match="cannot read file"):
        load_pipeline(root)


def test_config_empty_and_non_mapping_pipeline(project):
    assert "must be a mapping" in _problems(project, "- just\n- a list\n")        # regression: crashed
    root = project("", "jobs: []\n")
    with pytest.raises(ConfigError):
        load_pipeline(root)


def test_config_inline_jobs_and_shape_errors(project):
    root = project(BASIC_PIPELINE + "jobs:\n  - {id: inline, gate: commit, run: 'true'}\n", "jobs: []\n")
    assert load_pipeline(root).job("inline")
    text = _problems(project, BASIC_PIPELINE, "jobs: {id: x}\n")
    assert "`jobs` must be a list" in text
    text = _problems(project, BASIC_PIPELINE, "jobs:\n  - just a string\n")
    assert "a job must be a mapping" in text


def test_config_empty_job_file_is_ignored(project):
    root = project(BASIC_PIPELINE, "jobs: []\n")
    (root / ".gatewright" / "jobs" / "empty.yml").write_text("# nothing yet\n")
    assert load_pipeline(root).name == "t"


def test_config_gate_and_reference_errors(project):
    text = _problems(project, """\
version: 1
name: t
gates:
  - {id: a}
  - {id: a}
  - {id: b, requires: [zzz, c]}
  - {id: c}
policy:
  - {id: p, gates: [nope], metric: issues.total, op: "<=", value: 0}
vcs: {provider: gitea, repo: a/b, gates: [ghost]}
""")
    for words in ("duplicate gate ids", "requires unknown gate 'zzz'", "can only require gates listed before it",
                  "policy 'p' references unknown gate 'nope'", "vcs.gates lists unknown gate 'ghost'",
                  "vcs.api_url is required for Gitea/Forgejo"):
        assert words in text, words


# ---------------------------------------------------------------- scheduler
def _budget(**kw):
    base = dict(profile="server", max_parallel=4, cpu=2.0, mem=2 * 1024**3, max_cpu_percent=100.0,
                max_mem_percent=100.0, kill_mem_percent=100.0, nice=0)
    base.update(kw)
    return Budget(**base)


def _slow(job, handle):
    time.sleep(0.3)
    return JobResult(job.id, job.gate, job.category, PASSED)


@pytest.mark.parametrize("budget, jobs, words", [
    (_budget(cpu=1.5), [Job(id="a", gate="g", run="x", cpu=1),
                        Job(id="b", gate="g", run="x", cpu=1)], "cpu budget"),
    (_budget(max_mem_percent=0.0), [Job(id="a", gate="g", run="x"), Job(id="b", gate="g", run="x")], "system RAM"),
    (_budget(max_cpu_percent=0.0), [Job(id="a", gate="g", run="x"), Job(id="b", gate="g", run="x")], "system CPU")])
def test_scheduler_waits_for_resources(budget, jobs, words):
    waits = []
    s = Scheduler(budget, lambda k, d: waits.append(d.get("reason", "")) if k == "job_waiting" else None)
    res = s.run(jobs, _slow)
    assert all(r.status == PASSED for r in res.values()) and any(words in w for w in waits)
    assert s.peak_parallel == 1


def test_scheduler_survives_a_crashing_executor_and_external_deps():
    def boom(job, handle):
        raise RuntimeError("bug")
    res = Scheduler(_budget()).run([Job(id="a", gate="g", run="x", needs=["outside"])], boom)
    assert res["a"].status == "error" and "internal error" in res["a"].errors[0].human


def test_scheduler_cancel_skips_pending_jobs():
    stop = threading.Event()
    s = Scheduler(_budget(max_parallel=1), stop=stop)
    threading.Timer(0.1, stop.set).start()
    res = s.run([Job(id=f"j{i}", gate="g", run="x") for i in range(4)], _slow)
    assert [r.note for r in res.values() if r.status == SKIPPED] == ["cancelled"] * 3


# ---------------------------------------------------------------- templates
@pytest.mark.parametrize("files, expect", [({"package.json": "{}"}, "node"), ({"setup.py": ""}, "python"),
                                           ({"Dockerfile": "FROM x"}, "docker")])
def test_detect_template(tmp_path, files, expect):
    for n, c in files.items():
        (tmp_path / n).write_text(c)
    assert detect_template(tmp_path) == expect


def test_unknown_template(tmp_path):
    with pytest.raises(ValueError, match="Unknown template"):
        init(tmp_path, "cobol")


# ---------------------------------------------------------------- render + reports
def _render(renderable) -> str:
    c = Console(record=True, width=160)
    c.print(renderable)
    return c.export_text()


def _run(with_extras=True):
    run = RunResult(run_id="r1", pipeline="p", root="/x", started="2026-01-01")
    infra = JobError("build", "infra", "high", "Docker is not running.", "Start it.", raw="Cannot connect", tool="docker")
    warned = JobError("lint", "lint", "low", "Long line.", rule="E501", file="a.py", line=3)
    warned.suppression_reason = "suppression expired on 2026-01-01"
    sup = JobError("lint", "lint", "low", "Old finding.", rule="F401", file="b.py", line=1)
    sup.suppressed, sup.suppression_reason = True, "legacy code"
    g1 = GateResult(gate="build", mode="enforced")
    g1.status = FAILED
    j1 = JobResult("build", "build", "ops", "error")
    j1.errors = [infra]
    j2 = JobResult("lint", "build", "dev", FAILED)
    j2.errors = [warned, sup]
    g1.jobs = [j1, j2]
    g1.policy = [{"id": "x", "human": "no criticals", "passed": False, "skipped": False}]
    g2 = GateResult(gate="deploy", mode="enforced")
    g2.status = BLOCKED
    g3 = GateResult(gate="later", mode="advisory")
    g3.status = CANCELLED
    run.gates = [g1, g2, g3]
    run.status = FAILED
    if with_extras:
        run.warnings = ["something to know"]
        run.policy = [{"id": "r", "human": "coverage >= 80", "passed": True, "skipped": False}]
    return run, infra, warned


def test_terminal_rendering_of_infra_suppressed_and_empty_gates():
    run, infra, warned = _run()
    text = _render(error_panel(infra)) + _render(error_panel(warned)) + _render(summary_table(run))
    assert "Cannot connect" in text and "suppression expired" in text
    assert "+1 sup" in text and "deploy" in text and "CANCELLED" in text


def test_html_and_markdown_reports_cover_every_section(tmp_path):
    run, _, _ = _run()
    html = reports.render_html(run)
    for words in ("Cannot connect", "suppression expired", "Suppressed findings", "legacy code", "Warnings",
                  "something to know", "Policy", "coverage &gt;= 80", "s-cancelled"):
        assert words in html, words
    assert reports.render_markdown(run)
    run2, _, _ = _run(with_extras=False)
    assert "<b>Warnings</b>" not in reports.render_html(run2)


def test_pdf_without_weasyprint_or_pango(monkeypatch, tmp_path):
    import builtins
    real = builtins.__import__

    def no_weasy(name, *a, **k):
        if name == "weasyprint":
            raise ImportError("nope")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_weasy)
    assert "needs WeasyPrint" in reports.write_pdf("<p>x</p>", tmp_path / "r.pdf")

    def no_pango(name, *a, **k):
        if name == "weasyprint":
            raise OSError("cannot load library 'libpango-1.0-0': error")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_pango)
    msg = reports.write_pdf("<p>x</p>", tmp_path / "r.pdf")
    assert "Pango library" in msg
    run, _, _ = _run()
    files, warns = reports.write_reports(run, tmp_path / "out", ["md", "pdf"])
    assert any("Pango" in w for w in warns) and any(f.endswith("report.md") for f in files)


def test_lang_for_without_file():
    assert reports.lang_for(None) == "" and reports.lang_for("x.py") == "python"
