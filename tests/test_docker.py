# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Container mode tests. They run against a real Docker daemon.

Set GATEWRIGHT_TEST_IMAGE to an image that has `python` and `sh` (default: python:3.12-alpine).
Skipped automatically when Docker or the image is not available."""

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from gatewright.config import load_pipeline
from gatewright.engine import Engine, Selection
from gatewright.models import ERROR, FAILED, PASSED, SKIPPED

from .conftest import BASIC_PIPELINE

IMAGE = os.environ.get("GATEWRIGHT_TEST_IMAGE", "python:3.12-alpine")


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        if subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode != 0:
            return False
        return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True,
                              timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(not _docker_ready(), reason=f"Docker daemon or image {IMAGE} not available")


def _jobs(body: str) -> str:
    return body.replace("{IMG}", IMAGE)


def run(root, **kw):
    return Engine(load_pipeline(root)).run(Selection(reports=["md", "junit"], **kw))


def job(r, jid):
    return next(j for j in r.all_jobs() if j.job_id == jid)


def _containers(prefix: str) -> list[str]:
    out = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True).stdout
    return [n for n in out.split() if n.startswith(prefix)]


def test_container_job_passes_and_writes_output(project):
    root = project(BASIC_PIPELINE, _jobs("""\
        jobs:
          - id: build
            gate: commit
            image: {IMG}
            run: python -c "import os, platform; open(os.environ['GATEWRIGHT_OUT'] + '/result.txt', 'w').write('built in ' + platform.system()); print('job', os.environ['GATEWRIGHT_JOB'], 'cwd', os.getcwd())"
    """))
    r = run(root)
    b = job(r, "build")
    assert b.status == PASSED, b.errors
    run_dir = Path(r.artifact_dir)
    assert (run_dir / "out" / "build" / "result.txt").read_text(encoding="utf-8") == "built in Linux"
    log = (run_dir / "logs" / "build.log").read_text(encoding="utf-8")
    assert "job build cwd /work" in log
    assert b.tool_version == IMAGE
    assert not _containers(f"gatewright-{r.run_id}")  # --rm cleaned up


def test_container_failure_points_at_project_file(project):
    root = project(BASIC_PIPELINE, _jobs("""\
        jobs:
          - {id: bad, gate: commit, image: "{IMG}", run: "python boom.py"}
    """), files={"boom.py": "def f():\n    raise RuntimeError('inside container')\nf()\n"})
    r = run(root)
    e = job(r, "bad").errors[0]
    assert job(r, "bad").status == FAILED
    assert e.file == "boom.py" and e.line == 2, (e.file, e.line)
    assert any("raise RuntimeError" in t for n, t in e.snippet if n == 2)


def test_container_secrets_injected_and_masked(project, monkeypatch):
    monkeypatch.setenv("GATEWRIGHT_SECRET_DEPLOY_KEY", "container-secret-987")
    root = project(BASIC_PIPELINE, _jobs("""\
        jobs:
          - {id: s, gate: commit, image: "{IMG}", run: "echo key=$DEPLOY_KEY", secrets: [DEPLOY_KEY]}
    """))
    r = run(root)
    assert job(r, "s").status == PASSED
    log = (Path(r.artifact_dir) / "logs" / "s.log").read_text(encoding="utf-8")
    assert "key=***" in log
    for f in Path(r.artifact_dir).rglob("*"):
        if f.is_file():
            assert b"container-secret-987" not in f.read_bytes()


def test_container_network_none(project):
    root = project(BASIC_PIPELINE, _jobs("""\
        jobs:
          - id: offline
            gate: commit
            image: {IMG}
            network: false
            run: python -c "import socket; s=socket.socket(); s.settimeout(3); s.connect(('1.1.1.1', 53))"
    """))
    r = run(root)
    j = job(r, "offline")
    assert j.status in (ERROR, FAILED)
    assert "unreachable" in (j.errors[0].human + j.errors[0].raw).lower()


def test_container_memory_limit_kills_hog(project):
    root = project(BASIC_PIPELINE, _jobs("""\
        jobs:
          - id: hog
            gate: commit
            image: {IMG}
            resources: {mem: 64MB}
            run: python -c "x = bytearray(400 * 1024 * 1024); print(len(x))"
    """))
    r = run(root)
    j = job(r, "hog")
    assert j.status == ERROR
    assert j.errors[0].category == "resource", j.errors[0]
    assert j.exit_code == 137


def test_container_timeout_stops_container(project):
    root = project(BASIC_PIPELINE, _jobs("""\
        jobs:
          - {id: slow, gate: commit, image: "{IMG}", run: "sleep 60", timeout: 2}
    """))
    t0 = time.monotonic()
    r = run(root)
    assert time.monotonic() - t0 < 30
    j = job(r, "slow")
    assert j.status == ERROR and j.errors[0].category == "timeout"
    time.sleep(1)
    assert not _containers(f"gatewright-{r.run_id}"), "container left running after timeout"


def test_container_cancel_stops_container(project):
    root = project(BASIC_PIPELINE, _jobs("""\
        jobs:
          - {id: long, gate: commit, image: "{IMG}", run: "sleep 60"}
          - {id: after, gate: test, run: "echo x"}
    """))
    eng = Engine(load_pipeline(root))
    threading.Timer(3.0, eng.stop.set).start()
    t0 = time.monotonic()
    r = eng.run(Selection(reports=["md"]))
    assert time.monotonic() - t0 < 30
    assert job(r, "after").status == SKIPPED
    time.sleep(1)
    assert not _containers(f"gatewright-{r.run_id}")


def test_missing_image_is_explained(project):
    root = project(BASIC_PIPELINE, """\
        jobs:
          - {id: x, gate: commit, image: "gatewright-test/does-not-exist:1.0", run: "echo hi"}
    """)
    r = run(root)
    e = job(r, "x").errors[0]
    assert job(r, "x").status == ERROR and e.is_infra
    assert "gatewright-test/does-not-exist" in e.human and "not available" in e.human


def test_docker_daemon_down_is_explained(project, monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///tmp/gatewright-no-such-docker.sock")
    root = project(BASIC_PIPELINE, _jobs("""\
        jobs:
          - {id: x, gate: commit, image: "{IMG}", run: "echo hi"}
    """))
    r = run(root)
    e = job(r, "x").errors[0]
    assert job(r, "x").status == ERROR
    assert "Docker is not running" in e.human, e.human


def test_container_retry_uses_fresh_name(project):
    root = project(BASIC_PIPELINE, _jobs("""\
        jobs:
          - {id: net, gate: commit, image: "{IMG}", retries: 1, run: "echo 'Could not resolve host: x' >&2; exit 6"}
    """))
    r = run(root)
    j = job(r, "net")
    assert j.attempts == 2
    assert "Conflict" not in (Path(r.artifact_dir) / "logs" / "net.log").read_text(encoding="utf-8")
