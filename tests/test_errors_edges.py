# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Error/parsers edge cases: messy tool output must become a clear message, never a crash."""

import json

import pytest

from gatewright import errors
from gatewright.errors import ParseError, classify_infra, finalize, parse_output, relpath, snippet
from gatewright.models import Job, JobError


def _job(ptype, run="tool", **kw):
    return Job(id="j", gate="g", run=run, parser={"type": ptype}, **kw)


def test_shorten_and_loc():
    assert errors._shorten("short", 50) == "short"
    long_sentence = "First sentence is here. " + "word " * 40
    assert errors._shorten(long_sentence, 40) == "First sentence is here."
    assert errors._shorten("a " + "x" * 30 + " " + "y" * 60, 50).endswith("…")
    assert errors._loc("a.py", 3) == "`a.py` line 3" and errors._loc("a.py", None) == "`a.py`"
    assert errors._loc(None, None) == "the code"


def test_relpath_uris(tmp_path):
    assert relpath("file:///C:/src/a.py", str(tmp_path)) == "C:/src/a.py"
    assert relpath(f"file://{tmp_path}/a.py", str(tmp_path)) == "a.py"
    assert relpath("", str(tmp_path)) == ""


def test_snippet_and_finalize(tmp_path):
    assert snippet(str(tmp_path), None, 3) == [] and snippet(str(tmp_path), "missing.py", 3) == []
    e = finalize(JobError("j", "lint", "low", "h", raw="x" * (errors.MAX_RAW + 10)), str(tmp_path))
    assert e.raw.endswith("(truncated)") and e.fingerprint


def test_classify_not_executable_and_project_module(tmp_path):
    job = Job(id="j", gate="g", run="./tool")
    assert classify_infra(job, 126, "", False).category == "infra"
    pyt = Job(id="t", gate="g", run="pytest -q")
    assert classify_infra(pyt, 1, "ModuleNotFoundError: No module named 'myapp'", False) is None   # code bug
    other = Job(id="o", gate="g", run="ruff check")
    assert classify_infra(other, 1, "ModuleNotFoundError: No module named 'ruff'", False).category == "tool_missing"


def test_json_helpers():
    with pytest.raises(ParseError):
        errors._json("  ")
    assert errors._json('banner text\n{"a": 1}') == {"a": 1}
    with pytest.raises(ParseError):
        errors._json("x { not json [ also not")


def test_junit_invalid_xml_and_error_and_file_attrs(tmp_path):
    _, _, problem = parse_output(_job("junit"), "", "", 1, str(tmp_path), "<testsuite><oops")
    assert problem and "not valid XML" in problem.human
    (tmp_path / "t.py").write_text("x\n")
    xml = ('<testsuite><testcase classname="t" name="a" file="t.py" line="1"><error message="boom">trace</error>'
           '</testcase><testcase classname="t" name="b" file="t.py" line="0"><failure message="f"/></testcase>'
           '</testsuite>')
    errs, m, _ = parse_output(_job("junit"), "", "", 1, str(tmp_path), xml)
    assert "crashed before it could finish" in errs[0].human and errs[0].line == 1
    assert errs[1].line is None                           # regression: line="0" became line 1
    assert m["tests.failed"] == 2


def test_sarif_bad_security_severity(tmp_path):
    sarif = {"runs": [{"tool": {"driver": {"name": "x", "rules": [
        {"id": "R1", "properties": {"security-severity": "n/a"}},
        {"id": "R2", "properties": {"security-severity": None}}]}},
        "results": [{"ruleId": "R1", "level": "error", "message": {"text": "m"}},
                    {"ruleId": "R2", "level": "note", "message": {"text": "m"}}]}]}
    errs, _, problem = parse_output(_job("sarif"), json.dumps(sarif), "", 1, str(tmp_path), None)
    assert problem is None and [e.severity for e in errs] == ["high", "low"]   # regression: None crashed


def test_pip_audit(tmp_path):
    report = {"dependencies": [
        {"name": "jinja2", "version": "2.0", "vulns": [{"id": "GHSA-1", "fix_versions": ["3.1.4"],
                                                         "description": "XSS in templates."}]},
        {"name": "ok", "version": "1", "vulns": []},
        {"name": "local", "skip_reason": "not on PyPI"}]}
    errs, m, _ = parse_output(_job("pip-audit"), json.dumps(report), "", 1, str(tmp_path), None)
    assert m == {"deps.vulnerable": 1} and "3.1.4" in errs[0].hint and "GHSA-1" in errs[0].human
    errs, _, _ = parse_output(_job("pip-audit"), json.dumps([{"name": "a", "version": "1",
                              "vulns": [{"id": "X"}]}]), "", 1, str(tmp_path), None)   # old list format
    assert "no fixed version yet" in errs[0].hint


def test_mypy_windows_paths(tmp_path):
    out = "C:\\proj\\a.py:3: error: Incompatible types  [assignment]\nsrc/b.py:4:2: error: oops\n"
    errs, _, _ = parse_output(_job("mypy"), out, "", 1, str(tmp_path), None)
    assert [e.line for e in errs] == [3, 4] and errs[0].rule == "assignment"    # regression: C: dropped


def test_unknown_parser_and_parse_problems(tmp_path):
    _, _, problem = parse_output(_job("nope"), "", "", 1, str(tmp_path), None)
    assert problem.category == "config" and "Unknown parser" in problem.human
    assert parse_output(_job("pip-audit"), "", "", 0, str(tmp_path), None) == ([], {}, None)  # no output, exit 0
    _, _, problem = parse_output(_job("ruff"), '{"not": "a list"}', "", 1, str(tmp_path), None)
    assert problem and "could not understand" in problem.human
