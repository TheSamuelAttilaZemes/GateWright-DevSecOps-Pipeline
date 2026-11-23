# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
import json
import xml.etree.ElementTree as ET

from gatewright.errors import classify_infra, parse_output
from gatewright.models import Job, JobResult, RunResult, GateResult, JobError
from gatewright.reports import export_junit, export_sarif, render_html, render_markdown


def _job(parser, **kw):
    return Job(id="j", gate="g", run="tool", parser=parser if isinstance(parser, dict) else {"type": parser}, **kw)


def test_ruff(tmp_path):
    (tmp_path / "a.py").write_text("import os\n")
    out = json.dumps([{"code": "F401", "message": "`os` imported but unused", "filename": str(tmp_path / "a.py"),
                       "location": {"row": 1, "column": 8}}])
    errs, metrics, problem = parse_output(_job("ruff"), out, "", 1, str(tmp_path), None)
    assert problem is None and len(errs) == 1
    e = errs[0]
    assert e.file == "a.py" and e.line == 1 and e.severity == "medium"
    assert "never used" in e.human and e.snippet == [(1, "import os")]
    assert metrics["lint.issues"] == 1


def test_junit(tmp_path):
    (tmp_path / "test_x.py").write_text("def test_a():\n    assert 1 == 2\n")
    xml = f"""<testsuites><testsuite>
      <testcase classname="test_x" name="test_a"><failure message="assert 1 == 2">{tmp_path}/test_x.py:2: AssertionError</failure></testcase>
      <testcase classname="test_x" name="test_b"/>
      <testcase classname="test_x" name="test_c"><skipped/></testcase>
    </testsuite></testsuites>"""
    errs, m, _ = parse_output(_job({"type": "junit", "file": "r.xml"}), "", "", 1, str(tmp_path), xml)
    assert m == {"tests.total": 3, "tests.failed": 1, "tests.skipped": 1, "tests.passed": 1}
    assert errs[0].file == "test_x.py" and errs[0].line == 2
    assert "test_a" in errs[0].human and "assert 1 == 2" in errs[0].human


def test_sarif_security_severity(tmp_path):
    sarif = {"runs": [{"tool": {"driver": {"name": "Trivy", "rules": [
        {"id": "CVE-1", "properties": {"security-severity": "9.8"}, "help": {"text": "upgrade"}}]}},
        "results": [{"ruleId": "CVE-1", "level": "error", "message": {"text": "bad lib"},
                     "locations": [{"physicalLocation": {"artifactLocation": {"uri": "req.txt"},
                                                         "region": {"startLine": 3}}}]}]}]}
    errs, _, _ = parse_output(_job("sarif"), json.dumps(sarif), "", 1, str(tmp_path), None)
    assert errs[0].severity == "critical" and errs[0].tool == "trivy" and errs[0].line == 3


def test_gitleaks_is_critical(tmp_path):
    data = [{"RuleID": "aws-key", "Description": "AWS key", "File": "x.env", "StartLine": 1}]
    errs, _, _ = parse_output(_job("gitleaks"), "", "", 1, str(tmp_path), json.dumps(data))
    assert errs[0].severity == "critical" and errs[0].category == "secret" and "rotate" in errs[0].hint


def test_mypy_and_coverage(tmp_path):
    errs, _, _ = parse_output(_job("mypy"), "a.py:3: error: Incompatible types  [assignment]\n", "", 1,
                              str(tmp_path), None)
    assert errs[0].rule == "assignment" and errs[0].line == 3
    _, m, _ = parse_output(_job("coverage"), "", "", 0, str(tmp_path), '{"totals": {"percent_covered": 81.234}}')
    assert m["coverage.percent"] == 81.23


def test_bad_json_is_crash_not_finding(tmp_path):
    errs, _, problem = parse_output(_job("bandit"), "Traceback: something exploded", "", 2, str(tmp_path), None)
    assert not errs and problem.category == "crash" and problem.is_infra
    assert "could not read its results" in problem.human


def test_unknown_parser(tmp_path):
    _, _, problem = parse_output(_job("nope"), "", "", 0, str(tmp_path), None)
    assert problem.category == "config"


def test_fingerprint_survives_line_shift(tmp_path):
    from gatewright.errors import finalize
    (tmp_path / "a.py").write_text("x = 1\npassword = 'p'\n")
    e1 = finalize(JobError("j", "finding", "low", "h", file="a.py", line=2, rule="B105", tool="bandit"), str(tmp_path))
    (tmp_path / "a.py").write_text("# new line\nx = 1\npassword = 'p'\n")
    e2 = finalize(JobError("j", "finding", "low", "h", file="a.py", line=3, rule="B105", tool="bandit"), str(tmp_path))
    assert e1.fingerprint == e2.fingerprint


def test_infra_classification():
    j = Job(id="j", gate="g", run="docker build .")
    assert classify_infra(j, 127, "", False).category == "tool_missing"
    assert classify_infra(j, 1, "Cannot connect to the Docker daemon at unix:///var/run/docker.sock", False).category == "infra"
    assert classify_infra(j, 1, "write error: No space left on device", False).category == "resource"
    assert classify_infra(j, 137, "", False).category == "resource"
    assert classify_infra(j, 1, "AssertionError", False) is None


def _run_with_error():
    e = JobError("lint", "lint", "high", "`a.py` line 1 is bad", "fix it", file="a.py", line=1, rule="X1",
                 tool="ruff", snippet=[(1, "import os")], fingerprint="abc")
    jr = JobResult("lint", "commit", "dev", "failed", errors=[e], duration=1.0)
    ok = JobResult("unit", "commit", "dev", "passed")
    return RunResult("r1", "p", "/x", "now", status="failed",
                     gates=[GateResult("commit", "enforced", "failed", jobs=[jr, ok])])


def test_exports():
    run = _run_with_error()
    sarif = export_sarif(run)
    assert sarif["version"] == "2.1.0"
    res = sarif["runs"][0]["results"][0]
    assert res["locations"][0]["physicalLocation"]["region"]["startLine"] == 1
    tree = ET.fromstring(export_junit(run).split("\n", 1)[1])
    assert tree.find(".//testcase[@name='lint']/failure") is not None
    md = render_markdown(run)
    assert ">>    1 | import os" in md and "How to fix" in md
    html = render_html(run)
    assert 'class="hit"' in html and "<code>a.py</code>" in html
