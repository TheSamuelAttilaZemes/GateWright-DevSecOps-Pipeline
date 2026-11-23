# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Artifact signing with real ssh-keygen: sign, verify, and every way it should fail."""

import shutil
from pathlib import Path

import pytest

from gatewright import artifacts, signing
from gatewright.cli import main
from gatewright.config import load_pipeline
from gatewright.engine import Engine, Selection

from .conftest import BASIC_PIPELINE

pytestmark = pytest.mark.skipif(
    not shutil.which("ssh-keygen") or (signing.openssh_version() or (99, 0)) < (8, 2),
    reason="needs ssh-keygen from OpenSSH 8.2+")


@pytest.fixture
def signed_project(project, tmp_path_factory):
    keydir = tmp_path_factory.mktemp("keys")
    key = keydir / "signing_key"
    line = signing.keygen(key, "alice@example.com")
    pipe = BASIC_PIPELINE + f"signing:\n  key: {key}\n  identity: alice@example.com\n"
    root = project(pipe, 'jobs:\n  - {id: a, gate: commit, run: "echo hi"}\n')
    (root / ".gatewright" / "allowed_signers").write_text(line + "\n")
    return root, key


def run(root):
    return Engine(load_pipeline(root)).run(Selection(reports=["md", "html"]))


def test_runs_are_signed_and_verify(signed_project):
    root, _ = signed_project
    r = run(root)
    d = Path(r.artifact_dir)
    assert (d / "checksums.sha256.sig").exists() and (d / "signature.json").exists()
    assert artifacts.verify(d) == []                     # signature files don't count as "unexpected"
    ok, why = signing.verify(d, root / ".gatewright" / "allowed_signers")
    assert ok and "alice@example.com" in why
    assert main(["-C", str(root), "verify", r.run_id, "--require-signature"]) == 0


def test_changed_file_is_detected(signed_project):
    root, _ = signed_project
    d = Path(run(root).artifact_dir)
    (d / "report.md").write_text("I changed the report")
    assert "modified: report.md" in artifacts.verify(d)


def test_rewritten_checksums_break_the_signature(signed_project):
    """An attacker who edits a file AND recomputes checksums.sha256 is caught by the signature."""
    root, _ = signed_project
    d = Path(run(root).artifact_dir)
    (d / "report.md").write_text("forged")
    sig = (d / "checksums.sha256.sig").read_bytes()
    artifacts.write_checksums(d)                         # checksums now match the forged file
    (d / "checksums.sha256.sig").write_bytes(sig)
    assert artifacts.verify(d) == []
    ok, why = signing.verify(d, root / ".gatewright" / "allowed_signers")
    assert not ok and "does NOT match" in why


def test_unknown_signer_is_not_trusted(signed_project, tmp_path):
    root, _ = signed_project
    d = Path(run(root).artifact_dir)
    other = tmp_path / "other_key"
    other_line = signing.keygen(other, "bob@example.com")
    allowed = tmp_path / "allowed_signers"
    allowed.write_text(other_line + "\n")                 # only bob is trusted
    ok, why = signing.verify(d, allowed)
    assert not ok and "not trusted" in why


def test_unsigned_artifact_fails_when_required(project):
    root = project(BASIC_PIPELINE, 'jobs:\n  - {id: a, gate: commit, run: "echo hi"}\n')
    r = run(root)
    ok, why = signing.verify(Path(r.artifact_dir), root / ".gatewright" / "allowed_signers")
    assert not ok and "not signed" in why
    assert main(["-C", str(root), "verify", r.run_id, "--require-signature"]) == 1


def test_missing_key_is_a_warning_not_a_crash(project, tmp_path):
    pipe = BASIC_PIPELINE + f"signing:\n  key: {tmp_path / 'nope'}\n  identity: a@b\n"
    root = project(pipe, 'jobs:\n  - {id: a, gate: commit, run: "echo hi"}\n')
    r = run(root)
    assert r.status == "passed"
    assert any("NOT signed" in w and "gatewright keygen" in w for w in r.warnings)


def test_keygen_refuses_to_overwrite(tmp_path):
    key = tmp_path / "k"
    signing.keygen(key, "a@b")
    with pytest.raises(signing.SigningError, match="already exists"):
        signing.keygen(key, "a@b")


def test_edited_signer_name_is_detected(signed_project, tmp_path):
    root, _ = signed_project
    d = Path(run(root).artifact_dir)
    bob_line = signing.keygen(tmp_path / "bob", "bob@example.com")
    allowed = root / ".gatewright" / "allowed_signers"
    allowed.write_text(allowed.read_text(encoding="utf-8") + bob_line + "\n")
    meta = (d / "signature.json").read_text(encoding="utf-8").replace("alice@example.com", "bob@example.com")
    (d / "signature.json").write_text(meta)                  # claim bob signed it
    ok, why = signing.verify(d, allowed)
    assert not ok and "metadata was edited" in why
