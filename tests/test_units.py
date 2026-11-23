# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Focused unit tests for edge cases and error paths across modules
(written while reviewing every function for bugs; regressions are marked)."""

import json
import os
from datetime import date, timedelta

import pytest

from gatewright import artifacts
from gatewright.baseline import Baseline, Suppression
from gatewright.cache import Cache
from gatewright.graph import CycleError, levels, toposort, with_dependencies
from gatewright.history import History
from gatewright.models import (Job, JobError, JobResult, RunResult, GateResult, fmt_bytes, parse_mem)
from gatewright.policy import evaluate, metrics_for
from gatewright.models import PolicyRule
from gatewright.secrets import SecretStore



# ---------------------------------------------------------------- artifacts
def test_collect_outputs_copies_and_refuses_outside(tmp_path):
    root = tmp_path / "proj"
    (root / "dist").mkdir(parents=True)
    (root / "dist" / "app.whl").write_text("wheel")
    (root / ".gatewright").mkdir()
    (root / ".gatewright" / "secret.txt").write_text("no")
    (tmp_path / "outside.txt").write_text("x")
    run_dir = root / ".gatewright" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    copied = artifacts.collect_outputs(str(root), run_dir, "build",
                                       ["dist/*", "dist", "../outside.txt", str(tmp_path / "outside.txt"), ".gatewright/*"])
    assert copied == ["out/build/dist/app.whl"]               # regression: ../ and absolute paths crashed
    assert (run_dir / "out/build/dist/app.whl").read_text(encoding="utf-8") == "wheel"


def _bundle(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    (d / "a.txt").write_text("A")
    (d / "sub").mkdir()
    (d / "sub" / "b.txt").write_text("B")
    artifacts.write_checksums(d)
    return d


def test_verify_reports_every_kind_of_problem(tmp_path):
    d = _bundle(tmp_path)
    assert artifacts.verify(d) == []
    (d / "sub" / "b.txt").unlink()
    (d / "new.txt").write_text("?")
    cs = d / "checksums.sha256"
    cs.write_text(cs.read_text(encoding="utf-8") + "\n\nnot a checksum line\n" + "0" * 64 + "  ../../etc/passwd\n")
    problems = artifacts.verify(d)
    assert "missing: sub/b.txt" in problems and "unexpected file: new.txt" in problems
    assert any("malformed" in p for p in problems)                 # regression: crashed with ValueError
    assert any("outside the artifact" in p for p in problems)      # regression: hashed files outside
    cs.unlink()
    assert artifacts.verify(d) == ["checksums.sha256 is missing"]


def test_pack_and_manifest(tmp_path):
    d = _bundle(tmp_path)
    run = RunResult("r1", "p", str(tmp_path), "now")
    artifacts.write_manifest(run, d)
    assert json.loads((d / "manifest.json").read_text(encoding="utf-8"))["run_id"] == "r1"
    archive = artifacts.pack(d)
    assert archive.name == "gatewright-run-run.tar.gz" and archive.stat().st_size > 0


# ---------------------------------------------------------------- baseline
def _err(**kw):
    base = dict(job_id="sast", category="finding", severity="high", human="h", rule="B105", file="app/x.py",
                fingerprint="fp1")
    base.update(kw)
    return JobError(**base)


@pytest.mark.parametrize("supp, err, expected", [
    (Suppression("r", None, fingerprint="fp1"), _err(), True),
    (Suppression("r", None, fingerprint="other"), _err(), False),
    (Suppression("r", None, rule="B105"), _err(), True),
    (Suppression("r", None, rule="B101"), _err(), False),
    (Suppression("r", None), _err(), False),                                  # neither rule nor fingerprint
    (Suppression("r", None, rule="B105", file="tests/*"), _err(), False),
    (Suppression("r", None, rule="B105", file="app/*"), _err(), True),
    (Suppression("r", None, rule="B105", job="lint"), _err(), False),
    (Suppression("r", None, rule="B105", job="sast"), _err(), True),
])
def test_suppression_matching(supp, err, expected):
    assert supp.matches(err) is expected


def test_suppression_expiry():
    s = Suppression("r", date.today() - timedelta(days=1), rule="X")
    assert s.expired() and not Suppression("r", None, rule="X").expired()
    assert not s.expired(today=date.today() - timedelta(days=2))


def _baseline(tmp_path, text):
    (tmp_path / ".gatewright").mkdir(exist_ok=True)
    (tmp_path / ".gatewright" / "baseline.yml").write_text(text)
    return Baseline(str(tmp_path))


def test_baseline_bad_entries_are_warnings_not_crashes(tmp_path):
    b = _baseline(tmp_path, """\
suppressions:
  - just a string
  - {rule: B1}
  - {reason: no match key}
  - {rule: B2, reason: ok, expires: not-a-date}
  - {rule: B3, reason: ok, expires: 90}
  - {rule: B4, reason: ok, expires: 2030-01-31}
""")
    text = "\n".join(b.warnings)
    assert "must be a mapping" in text and "`reason` is required" in text
    assert "needs `fingerprint` or `rule`" in text and text.count("bad date") == 2
    assert [s.rule for s in b.items] == ["B2", "B3", "B4"]
    assert b.items[2].expires == date(2030, 1, 31) and b.items[1].expires is None
    for s in b.items:
        s.expired()                                   # regression: int expiry raised TypeError here


@pytest.mark.parametrize("text, warns", [("- a\n- b\n", True), ("suppressions: oops\n", True),
                                         ("suppressions:\n", False), ("x: [\n", True), ("", False)])
def test_baseline_malformed_files(tmp_path, text, warns):
    b = _baseline(tmp_path, text)
    assert b.items == [] and bool(b.warnings) is warns


def test_baseline_apply_skips_infra_and_handles_expired(tmp_path):
    b = _baseline(tmp_path, "suppressions:\n  - {rule: B105, reason: old, expires: 2000-01-01}\n"
                            "  - {fingerprint: fp9, reason: fine}\n")
    infra = _err(category="timeout", rule="B105")
    b.apply(infra)
    assert not infra.suppressed
    e = _err()
    b.apply(e)
    assert not e.suppressed and "expired" in e.suppression_reason and b.expired_hits
    ok = _err(fingerprint="fp9", rule="Z")
    b.apply(ok)
    assert ok.suppressed and ok.suppression_reason == "fine"


def test_baseline_write_from_run_keeps_existing(tmp_path):
    (tmp_path / ".gatewright").mkdir()
    errs = [_err(fingerprint="a"), _err(fingerprint="b", suppressed=True), _err(fingerprint="c", category="timeout")]
    run = RunResult("r", "p", str(tmp_path), "now",
                    gates=[GateResult("commit", "enforced", jobs=[JobResult("sast", "commit", "sec", "failed", errors=errs)])])
    path, n = Baseline.write_from_run(str(tmp_path), run, 10, "why")
    assert n == 1
    _, n2 = Baseline.write_from_run(str(tmp_path), run, 10, "why")
    assert n2 == 0 and len(Baseline(str(tmp_path)).items) == 1


# ---------------------------------------------------------------- cache
def test_cache_unreadable_and_corrupt_files(tmp_path):
    (tmp_path / "f.txt").write_text("x")
    c = Cache(str(tmp_path))
    job = Job(id="j", gate="g", run="x", inputs=["f.txt"])
    key = c.key(job, "v1")
    assert c.get(job, key) is None                                  # nothing cached yet
    c.put(job, key, JobResult("j", "g", "dev", "failed"))
    assert c.get(job, key) is None                                  # failures are never cached
    c.put(job, key, JobResult("j", "g", "dev", "passed"))
    assert c.get(job, key).status == "passed"
    assert c.get(job, "other-key") is None
    for junk in ("{not json", "[1, 2]", '{"key": "%s"}' % key, '{"key": "%s", "result": {"bogus": 1}}' % key):
        (c.dir / "j.json").write_text(junk)
        assert c.get(job, key) is None                              # regression: list/odd JSON crashed
    assert c.clear() == 1 and c.clear() == 0
    os.chmod(tmp_path / "f.txt", 0)
    try:
        if not os.access(tmp_path / "f.txt", os.R_OK):              # (root can still read it)
            assert Cache(str(tmp_path)).key(job, "v1") != key       # unreadable file hashes differently
    finally:
        os.chmod(tmp_path / "f.txt", 0o644)


# ---------------------------------------------------------------- secrets
def test_secrets_file_formats_and_permissions(tmp_path, monkeypatch):
    (tmp_path / ".gatewright").mkdir()
    f = tmp_path / ".gatewright" / "secrets.env"
    f.write_text('# comment\n\nA=plain\nexport B="quoted value"\nC=\'single\'\nD=abc"\nnot a line\n')
    os.chmod(f, 0o644)
    s = SecretStore(str(tmp_path))
    if os.name == "posix":
        assert any("readable by other users" in w for w in s.warnings)
    assert [s.get(k) for k in "ABCD"] == ["plain", "quoted value", "single", 'abc"']   # regression: export/quotes
    monkeypatch.setenv("GATEWRIGHT_SECRET_A", "from-env")
    assert SecretStore(str(tmp_path)).get("A") == "from-env"
    env, missing = s.resolve(["A", "NOPE"])
    assert missing == ["NOPE"]
    assert s.mask("plain quoted value abc\" from-env") == "*** *** *** ***"   # regression: old values stay masked
    assert s.mask("") == ""
    off = SecretStore(str(tmp_path), enabled=False)
    assert off.get("A") is None


# ---------------------------------------------------------------- models / graph / policy / history
@pytest.mark.parametrize("value, expected", [(None, 256 * 1024**2), (2, 2 * 1024**2), ("1.5GB", int(1.5 * 1024**3)),
                                             ("512 mb", 512 * 1024**2), ("64K", 64 * 1024), ("10", 10 * 1024**2)])
def test_parse_mem(value, expected):
    assert parse_mem(value) == expected


def test_fmt_bytes_and_tool_name():
    assert [fmt_bytes(x) for x in (10, 2048, 3 * 1024**2, 5 * 1024**5)] == ["10B", "2KB", "3.0MB", "5120.0TB"]
    assert Job(id="a", gate="g", run="python -m pytest -q").tool_name == "pytest"
    assert Job(id="a", gate="g", run="python -m").tool_name == "python"
    assert Job(id="a", gate="g", run="   ").tool_name == ""
    assert Job(id="a", gate="g", run="x", tool="ruff").tool_name == "ruff"


def test_graph_helpers():
    a, b, c = Job(id="a", gate="g", run="x"), Job(id="b", gate="g", run="x", needs=["a", "ext"]), \
        Job(id="c", gate="g", run="x", needs=["b"])
    assert [j.id for j in toposort([c, b, a])] == ["a", "b", "c"]
    assert [[j.id for j in w] for w in levels([a, b, c])] == [["a"], ["b"], ["c"]]
    assert with_dependencies({"c", "zzz"}, [a, b, c]) == {"a", "b", "c"}
    with pytest.raises(CycleError) as e:
        toposort([Job(id="x", gate="g", run="r", needs=["y"]), Job(id="y", gate="g", run="r", needs=["x"])])
    assert e.value.cycle[0] == e.value.cycle[-1]


def test_policy_scopes_and_metrics():
    jr = JobResult("t", "test", "dev", "passed", metrics={"coverage.percent": 91.0, "tests.failed": 0},
                   errors=[_err(severity="low"), _err(severity="high", suppressed=True), _err(category="timeout")])
    m = metrics_for([jr])
    assert m["findings.low"] == 1 and m["findings.suppressed"] == 1 and m["findings.high"] == 0
    rules = [PolicyRule("cov", "coverage.percent", ">=", 90, gates=["test"]),
             PolicyRule("other-gate", "findings.low", "==", 0, gates=["commit"]),
             PolicyRule("run-level", "findings.low", "==", 0),
             PolicyRule("missing", "nope.metric", "==", 0, gates=["test"])]
    gate = {r["id"]: r for r in evaluate(rules, [jr], "test")}
    assert set(gate) == {"cov", "missing"} and gate["cov"]["passed"] and gate["missing"]["skipped"]
    run = {r["id"]: r for r in evaluate(rules, [jr], None)}
    assert set(run) == {"run-level"} and not run["run-level"]["passed"]


def test_history_prefix_and_previous(tmp_path):
    h = History(str(tmp_path))
    for i in range(3):
        h.save(RunResult(f"2026-run-{i}", "p", str(tmp_path), f"t{i}"))
    assert h.load("previous").run_id == "2026-run-1" and h.load("2026-run-0").run_id == "2026-run-0"
    assert h.load("does-not-exist") is None
    assert History(str(tmp_path / "empty")).load() is None
