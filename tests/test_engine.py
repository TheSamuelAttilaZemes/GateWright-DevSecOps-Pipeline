# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
import json
import os
import threading
import time
from pathlib import Path

from gatewright.artifacts import verify
from gatewright.config import load_pipeline
from gatewright.engine import Engine, Selection
from gatewright.hardware import Budget
from gatewright.models import BLOCKED, CACHED, ERROR, FAILED, PASSED, REUSED, SKIPPED, WARN, Job, JobResult
from gatewright.scheduler import Scheduler

from .conftest import BASIC_PIPELINE, needs_posix_shell

FAIL_SCRIPT = """\
import sys
def main():
    raise ValueError("boom")  # line 3
main()
"""


def run(root, **kw):
    sel = Selection(reports=kw.pop("reports", ["md", "sarif", "junit"]), **kw)
    return Engine(load_pipeline(root)).run(sel)


def job(run_result, jid) -> JobResult:
    return next(j for j in run_result.all_jobs() if j.job_id == jid)


def test_pass_fail_and_enforced_gate_blocks(project):
    root = project(BASIC_PIPELINE, """\
        jobs:
          - {id: ok, gate: commit, run: "echo hello"}
          - {id: bad, gate: commit, run: "{PY} boom.py"}
          - {id: later, gate: test, run: "echo never"}
    """, files={"boom.py": FAIL_SCRIPT})
    r = run(root)
    assert job(r, "ok").status == PASSED
    bad = job(r, "bad")
    assert bad.status == FAILED
    e = bad.errors[0]
    assert e.file == "boom.py" and e.line == 3
    assert any(n == 3 and "raise ValueError" in t for n, t in e.snippet)
    assert "boom" in e.human
    assert job(r, "later").status == BLOCKED
    assert r.status == FAILED
    run_dir = Path(r.artifact_dir)
    for f in ("manifest.json", "report.md", "gate-commit.md", "results.sarif", "junit.xml", "checksums.sha256",
              "logs/ok.log"):
        assert (run_dir / f).exists(), f
    assert (run_dir.parent / f"gatewright-run-{r.run_id}.tar.gz").exists()
    assert "hello" in (run_dir / "logs/ok.log").read_text(encoding="utf-8")
    assert verify(run_dir) == []
    (run_dir / "report.md").write_text("tampered")
    assert "modified: report.md" in verify(run_dir)


def test_advisory_gate_continues(project):
    pipe = BASIC_PIPELINE.replace("  - id: commit\n    mode: enforced", "  - id: commit\n    mode: advisory")
    root = project(pipe, """\
        jobs:
          - {id: bad, gate: commit, run: "exit 3"}
          - {id: later, gate: test, run: "echo ran"}
    """)
    r = run(root)
    assert job(r, "bad").status == FAILED
    assert job(r, "later").status == PASSED
    assert r.status == WARN


def test_dependency_failure_blocks(project):
    root = project(BASIC_PIPELINE, """\
        jobs:
          - {id: a, gate: commit, run: "exit 1"}
          - {id: b, gate: commit, run: "echo b", needs: [a]}
    """)
    r = run(root)
    assert job(r, "b").status == BLOCKED
    assert "a" in job(r, "b").note


@needs_posix_shell
def test_timeout_is_infra_error(project):
    root = project(BASIC_PIPELINE, """\
        jobs:
          - {id: slow, gate: commit, run: "sleep 30", timeout: 1}
    """)
    t0 = time.monotonic()
    r = run(root)
    assert time.monotonic() - t0 < 15
    j = job(r, "slow")
    assert j.status == ERROR
    assert j.errors[0].category == "timeout" and j.errors[0].is_infra
    assert "1 seconds" in j.errors[0].human


def test_missing_tool(project):
    root = project(BASIC_PIPELINE, """\
        jobs:
          - {id: need, gate: commit, run: "definitely-not-a-tool-xyz --scan"}
          - {id: opt, gate: commit, run: "another-missing-tool-xyz", optional: true}
    """)
    r = run(root)
    assert job(r, "need").status == ERROR
    assert job(r, "need").errors[0].category == "tool_missing"
    assert "gatewright doctor" in job(r, "need").errors[0].hint
    assert job(r, "opt").status == SKIPPED


@needs_posix_shell
def test_retries_on_network_error(project):
    root = project(BASIC_PIPELINE, """\
        jobs:
          - {id: net, gate: commit, run: "echo 'curl: Could not resolve host: x' >&2; exit 6", retries: 1}
    """)
    r = run(root)
    j = job(r, "net")
    assert j.attempts == 2
    assert j.status == ERROR and j.errors[0].category == "infra"


@needs_posix_shell
def test_secrets_are_masked(project, monkeypatch):
    monkeypatch.setenv("GATEWRIGHT_SECRET_API_TOKEN", "s3cr3t-value-123")
    root = project(BASIC_PIPELINE, """\
        jobs:
          - {id: uses, gate: commit, run: "echo token=$API_TOKEN; exit 1", secrets: [API_TOKEN]}
          - {id: missing, gate: commit, run: "echo x", secrets: [NOT_SET_ANYWHERE]}
    """)
    r = run(root)
    run_dir = Path(r.artifact_dir)
    for f in run_dir.rglob("*"):
        if f.is_file():
            assert b"s3cr3t-value-123" not in f.read_bytes(), f
    assert "token=***" in (run_dir / "logs/uses.log").read_text(encoding="utf-8")
    m = job(r, "missing")
    assert m.status == ERROR and "NOT_SET_ANYWHERE" in m.errors[0].human


@needs_posix_shell
def test_cache_and_invalidation(project):
    root = project(BASIC_PIPELINE, """\
        jobs:
          - {id: c, gate: commit, run: "cat src.txt", inputs: ["src.txt"]}
    """, files={"src.txt": "v1"})
    assert job(run(root), "c").status == PASSED
    assert job(run(root), "c").status == CACHED
    assert job(run(root, use_cache=False), "c").status == PASSED
    (root / "src.txt").write_text("v2")
    assert job(run(root), "c").status == PASSED


@needs_posix_shell
def test_rerun_failed_reuses_passed_jobs(project):
    root = project(BASIC_PIPELINE, """\
        jobs:
          - {id: good, gate: commit, run: "echo ok", cache: false}
          - {id: flaky, gate: commit, run: "test -f fixed.flag", cache: false}
    """)
    first = run(root)
    assert job(first, "flaky").status == FAILED
    (root / "fixed.flag").write_text("")
    eng = Engine(load_pipeline(root))
    second = eng.rerun_failed(first.run_id)
    assert second.resumed_from == first.run_id
    assert job(second, "good").status == REUSED
    assert job(second, "flaky").status == PASSED
    assert second.status == PASSED


def test_policy_and_baseline(project):
    pipe = BASIC_PIPELINE + """\
policy:
  - id: no-high
    gates: [commit]
    metric: findings.high
    op: "=="
    value: 0
"""
    root = project(pipe, """\
        jobs:
          - {id: scan, gate: commit, run: "{PY} -c \\"print(open('bandit.json').read()); raise SystemExit(1)\\"", parser: bandit}
    """, files={"bandit.json": '{"results": [{"test_id": "B602", "issue_severity": "HIGH", "filename": "app.py", '
                                '"line_number": 2, "issue_text": "shell=True"}]}',
                "app.py": "import subprocess\nsubprocess.run('ls', shell=True)\n"})
    r1 = run(root)
    scan = job(r1, "scan")
    assert scan.status == FAILED
    e = scan.errors[0]
    assert e.rule == "B602" and e.severity == "high" and "shell=True" in e.human
    assert r1.gates[0].policy[0]["passed"] is False

    from gatewright.baseline import Baseline
    _, n = Baseline.write_from_run(str(root), r1, days=30, reason="legacy code, ticket #12")
    assert n == 1
    r2 = run(root, use_cache=False)
    scan2 = job(r2, "scan")
    assert scan2.status == PASSED and scan2.errors[0].suppressed
    assert r2.gates[0].policy[0]["passed"] is True
    assert r2.status == PASSED

    # expired suppression -> active again, with a warning
    bl = root / ".gatewright" / "baseline.yml"
    bl.write_text(bl.read_text(encoding="utf-8").replace(bl.read_text(encoding="utf-8").split("expires: ")[1].split("\n")[0], "'2020-01-01'"))
    r3 = run(root, use_cache=False)
    assert job(r3, "scan").status == FAILED
    assert any("expired" in w for w in r3.warnings)


def test_history_and_diff(project):
    from gatewright.history import History, diff
    root = project(BASIC_PIPELINE, """\
        jobs:
          - {id: g, gate: commit, run: "{PY} boom.py", cache: false}
    """, files={"boom.py": FAIL_SCRIPT})
    r1 = run(root)
    (root / "boom.py").write_text("print('fine')\n")
    r2 = run(root)
    h = History(str(root))
    assert [row["id"] for row in h.list()] == [r2.run_id, r1.run_id]
    d = diff(h.load(r1.run_id), h.load(r2.run_id))
    assert len(d["fixed"]) == 1 and not d["new"]
    assert "1 fixed" in (Path(r2.artifact_dir) / "report.md").read_text(encoding="utf-8")


def test_gate_requires_previous_pass(project):
    pipe = BASIC_PIPELINE + "  - id: staging\n    mode: enforced\n    requires: [test]\n"
    root = project(pipe, """\
        jobs:
          - {id: t, gate: test, run: "echo test"}
          - {id: deploy, gate: staging, run: "echo deploy"}
    """, git=True)
    r1 = run(root, gates=["staging"])
    assert job(r1, "deploy").status == BLOCKED
    assert "requires" in job(r1, "deploy").note
    run(root, gates=["test"])
    r3 = run(root, gates=["staging"])
    assert job(r3, "deploy").status == PASSED


def test_selection_by_category_and_job(project):
    root = project(BASIC_PIPELINE, """\
        jobs:
          - {id: lint, gate: commit, category: dev, run: "echo l"}
          - {id: sast, gate: commit, category: sec, run: "echo s", needs: [lint]}
          - {id: unit, gate: test, category: dev, run: "echo u"}
    """)
    r = run(root, categories=["sec"])
    assert sorted(j.job_id for j in r.all_jobs()) == ["lint", "sast"]  # dependency included
    r = run(root, jobs=["unit"])
    assert [j.job_id for j in r.all_jobs()] == ["unit"]


def test_dry_run_plan(project):
    root = project(BASIC_PIPELINE, """\
        jobs:
          - {id: a, gate: commit, run: "echo a"}
          - {id: b, gate: commit, run: "echo b", needs: [a], resources: {mem: 999GB}}
    """)
    plan = Engine(load_pipeline(root)).plan()
    assert [[j.id for j in w] for w in plan.gates[0].waves] == [["a"], ["b"]]
    assert any("run alone" in w for w in plan.warnings)
    assert not (root / ".gatewright" / "runs").exists()


def _budget(**kw):
    b = dict(profile="test", max_parallel=2, cpu=8, mem=8 * 1024**3, max_cpu_percent=100,
             max_mem_percent=100, kill_mem_percent=100, nice=0)
    b.update(kw)
    return Budget(**b)


def test_scheduler_respects_max_parallel():
    active, peak = [0], [0]
    lock = threading.Lock()

    def execute(j, handle):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.3)
        with lock:
            active[0] -= 1
        return JobResult(j.id, j.gate, j.category, PASSED)

    jobs = [Job(id=f"j{i}", gate="g", run="x") for i in range(5)]
    res = Scheduler(_budget(max_parallel=2)).run(jobs, execute)
    assert len(res) == 5 and peak[0] == 2


def test_scheduler_memory_budget_serializes():
    peak = [0, 0]

    def execute(j, handle):
        peak[0] += 1
        peak[1] = max(peak[1], peak[0])
        time.sleep(0.2)
        peak[0] -= 1
        return JobResult(j.id, j.gate, j.category, PASSED)

    jobs = [Job(id=f"j{i}", gate="g", run="x", mem=3 * 1024**3) for i in range(3)]
    Scheduler(_budget(max_parallel=4, mem=4 * 1024**3)).run(jobs, execute)
    assert peak[1] == 1  # 2 x 3GB does not fit in 4GB


@needs_posix_shell
def test_watchdog_kills_job(tmp_path):
    from gatewright.runner import execute as run_job
    from gatewright.secrets import SecretStore
    (tmp_path / ".gatewright").mkdir()
    j = Job(id="hog", gate="g", run="sleep 20", timeout=60)

    def execute(job, handle):
        return run_job(job, root=str(tmp_path), run_id="r", out_dir=str(tmp_path / "o"),
                       log_file=str(tmp_path / "l.log"), secrets=SecretStore(str(tmp_path)), handle=handle)

    t0 = time.monotonic()
    res = Scheduler(_budget(kill_mem_percent=1)).run([j], execute)["hog"]
    assert time.monotonic() - t0 < 10
    assert res.status == ERROR and res.errors[0].category == "resource"
    assert "protect the machine" in res.errors[0].human


@needs_posix_shell
def test_cancel_stops_run(project):
    root = project(BASIC_PIPELINE, """\
        jobs:
          - {id: long, gate: commit, run: "sleep 20"}
          - {id: after, gate: test, run: "echo x"}
    """)
    eng = Engine(load_pipeline(root))
    threading.Timer(1.0, eng.stop.set).start()
    t0 = time.monotonic()
    r = eng.run(Selection(reports=["md"]))
    assert time.monotonic() - t0 < 10
    assert job(r, "after").status == SKIPPED


def test_manifest_has_no_env_values(project, monkeypatch):
    monkeypatch.setenv("SOME_PASSWORD", "pw-should-not-leak")
    root = project(BASIC_PIPELINE, 'jobs:\n  - {id: a, gate: commit, run: "echo hi"}\n')
    r = run(root)
    manifest = json.loads((Path(r.artifact_dir) / "manifest.json").read_text(encoding="utf-8"))
    assert "pw-should-not-leak" not in json.dumps(manifest)
    assert manifest["gates"][0]["jobs"][0]["status"] == PASSED
    assert os.path.isabs(manifest["artifact_dir"])


def test_when_exists_skips_job_without_files(project):
    root = project(BASIC_PIPELINE, """\
        jobs:
          - {id: audit, gate: commit, run: "exit 1", when_exists: ["requirements*.txt"]}
          - {id: other, gate: commit, run: "echo ok"}
    """)
    r = run(root)
    assert job(r, "audit").status == SKIPPED and "requirements*.txt" in job(r, "audit").note
    assert r.status == PASSED
    (root / "requirements-dev.txt").write_text("x\n")
    assert job(run(root, use_cache=False), "audit").status == FAILED
