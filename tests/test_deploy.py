# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Environments: promote, ordering rules, approval, verify, auto-rollback, rollback, release notes."""

import textwrap
from pathlib import Path

import pytest

from gatewright.config import load_pipeline
from gatewright.deploy import Deployer, DeployError
from gatewright.engine import Engine, Selection

from .conftest import PY  # noqa: E402

PIPELINE = """\
version: 1
name: shop
gates:
  - id: commit
  - id: test
environments:
  staging:
    deploy: '{py} deploy.py'
    verify: '{py} verify.py'
    requires: [test]
    auto_rollback: true
    release_notes: true
  production:
    deploy: '{py} deploy.py'
    after: [staging]
    approval: true
"""

JOBS = f"""\
jobs:
  - {{id: check, gate: commit, run: '{PY} check.py', cache: false}}
  - {{id: unit, gate: test, run: '{PY} -c "print(1)"', cache: false}}
"""

DEPLOY = """\
import os, pathlib
server = pathlib.Path("server") / os.environ["GATEWRIGHT_ENV"]
server.mkdir(parents=True, exist_ok=True)
assert pathlib.Path(os.environ["GATEWRIGHT_ARTIFACT"], "manifest.json").exists()
(server / "live.txt").write_text(os.environ["GATEWRIGHT_DEPLOY_RUN_ID"])
print("deployed", os.environ["GATEWRIGHT_DEPLOY_RUN_ID"], "to", os.environ["GATEWRIGHT_ENV"])
"""

# The health check looks at what is LIVE: it fails only while the run named in UNHEALTHY is deployed.
VERIFY = """\
import os, pathlib, sys
live = (pathlib.Path("server") / os.environ["GATEWRIGHT_ENV"] / "live.txt").read_text(encoding="utf-8")
bad = pathlib.Path("UNHEALTHY").read_text(encoding="utf-8") if pathlib.Path("UNHEALTHY").exists() else None
sys.exit("health check failed: HTTP 500" if live == bad else 0)
"""
CHECK = "import os, sys\nsys.exit(1 if os.path.exists('BROKEN') else 0)\n"


@pytest.fixture
def shop(project):
    root = project(PIPELINE.format(py=PY), JOBS,
                   files={"deploy.py": DEPLOY, "verify.py": VERIFY, "check.py": CHECK}, git=True)
    return root


def run(root):
    return Engine(load_pipeline(root)).run(Selection(reports=["md"]))


def deployer(root, answer=False):
    return Deployer(load_pipeline(root), confirm=lambda q: answer)


def live(root, env):
    return (root / "server" / env / "live.txt").read_text(encoding="utf-8")


def test_promote_through_environments(shop):
    r1 = run(shop)
    d = deployer(shop)
    with pytest.raises(DeployError, match="has not been deployed to `staging`"):
        d.promote(r1.run_id, "production", yes=True)
    dep = d.promote(r1.run_id, "staging")
    assert dep.status == "succeeded" and live(shop, "staging") == r1.run_id
    assert (Path(dep.log_dir) / "deploy.log").read_text(encoding="utf-8").count("deployed") == 1
    assert (Path(dep.log_dir) / "release-notes.md").exists()
    with pytest.raises(DeployError, match="requires approval"):
        d.promote(r1.run_id, "production")                   # confirm() answers no
    assert deployer(shop, answer=True).promote(r1.run_id, "production").status == "succeeded"
    assert live(shop, "production") == r1.run_id
    assert d.current("production").run_id == r1.run_id


def test_failed_run_and_missing_gate_are_refused(shop):
    (shop / "BROKEN").write_text("x")
    bad = run(shop)
    with pytest.raises(DeployError) as e:
        deployer(shop).promote(bad.run_id, "staging")
    assert "FAILED" in str(e.value) and "gate `test`" in str(e.value)


def test_tampered_artifact_is_refused(shop):
    r = run(shop)
    Path(r.artifact_dir, "report.md").write_text("changed")
    with pytest.raises(DeployError, match="modified"):
        deployer(shop).promote(r.run_id, "staging")


def test_failed_verify_rolls_back_automatically(shop):
    d = deployer(shop)
    good = run(shop)
    d.promote(good.run_id, "staging")
    (shop / "new.txt").write_text("v2")
    newer = run(shop)
    (shop / "UNHEALTHY").write_text(newer.run_id)             # the new version's health check fails
    dep = d.promote(newer.run_id, "staging")
    assert dep.status == "rolled_back" and "health check failed" in dep.note
    assert live(shop, "staging") == good.run_id               # the previous artifact is live again
    assert d.current("staging").run_id == good.run_id
    kinds = [(x.kind, x.run_id, x.status) for x in d.deployments("staging")]
    assert kinds[0] == ("rollback", good.run_id, "succeeded")


def test_manual_rollback(shop):
    d = deployer(shop)
    first = run(shop)
    with pytest.raises(DeployError, match="Nothing to roll back to"):
        d.promote(first.run_id, "staging") and d.rollback("staging")
    (shop / "v2.txt").write_text("2")
    second = run(shop)
    d.promote(second.run_id, "staging")
    assert live(shop, "staging") == second.run_id
    back = d.rollback("staging")
    assert back.kind == "rollback" and back.run_id == first.run_id and live(shop, "staging") == first.run_id


def test_unknown_environment_and_config_checks(shop, project, tmp_path):
    with pytest.raises(DeployError, match="Unknown environment `prod`"):
        deployer(shop).promote(None, "prod")
    from gatewright.config import ConfigError
    bad = textwrap.dedent("""\
        gates: [{id: commit}]
        environments:
          a: {deploy: x, requires: [nope], after: [a]}
          b: {deploy: y, after: [zz]}
    """)
    (shop / ".gatewright" / "pipeline.yml").write_text(bad)
    with pytest.raises(ConfigError) as e:
        load_pipeline(shop)
    text = "\n".join(e.value.problems)
    assert "requires unknown gate 'nope'" in text and "after itself" in text and "unknown environment 'zz'" in text


def test_cli_promote_and_deployments(shop, capsys):
    from gatewright.cli import main
    r = run(shop)
    assert main(["-C", str(shop), "promote", r.run_id, "--to", "staging"]) == 0
    assert main(["-C", str(shop), "deployments"]) == 0
    out = capsys.readouterr().out
    assert "staging: live = " + r.run_id in out
    assert main(["-C", str(shop), "promote", r.run_id, "--to", "production"]) == 2   # no tty, no --yes
    assert main(["-C", str(shop), "promote", r.run_id, "--to", "production", "--yes"]) == 0
