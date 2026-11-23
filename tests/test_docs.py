# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Keep the documentation honest: every option, parser, command and key is
documented, and every link between the docs resolves (including #anchors)."""

import re
from pathlib import Path

import pytest

from gatewright.cli import build_parser
from gatewright.config import JOB_SCHEMA, PIPELINE_SCHEMA
from gatewright.errors import PARSERS
from gatewright.tui import GatewrightApp

ROOT = Path(__file__).resolve().parents[1]
DOCS = sorted([ROOT / "README.md", ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md", *(ROOT / "docs").glob("*.md")])
CONFIG_DOC = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")


def _keys():
    p = PIPELINE_SCHEMA["properties"]
    yield from JOB_SCHEMA["properties"]
    yield from p
    yield from p["gates"]["items"]["properties"]
    yield from p["limits"]["properties"]
    yield from p["policy"]["items"]["properties"]
    yield from JOB_SCHEMA["properties"]["resources"]["properties"]
    yield from p["vcs"]["properties"]
    yield from p["vcs"]["properties"]["webhook"]["properties"]
    yield from p["signing"]["properties"]
    yield from p["environments"]["additionalProperties"]["properties"]
    yield from p["notify"]["properties"]
    yield from p["notify"]["properties"]["channels"]["items"]["properties"]


def test_every_notify_event_is_documented():
    from gatewright.config import NOTIFY_EVENTS
    for ev in NOTIFY_EVENTS:
        assert f"`{ev}`" in CONFIG_DOC, ev


@pytest.mark.parametrize("key", sorted(set(_keys())))
def test_every_config_key_is_documented(key):
    assert f"`{key}`" in CONFIG_DOC or f"`resources.{key}`" in CONFIG_DOC, f"{key} missing from docs/configuration.md"


@pytest.mark.parametrize("name", sorted(PARSERS))
def test_every_parser_is_documented(name):
    assert f"`{name}`" in CONFIG_DOC


def test_every_cli_command_is_documented():
    sub = next(a for a in build_parser()._actions if a.__class__.__name__ == "_SubParsersAction")
    text = "".join(p.read_text(encoding="utf-8") for p in DOCS)
    missing = [c for c in sub.choices if f"gatewright {c}" not in text and c != "resume"]
    assert not missing, f"commands not mentioned in the docs: {missing}"


def test_readme_lists_every_command_and_option():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    table = readme[readme.index("### All commands"):readme.index("## Documentation\n")]
    sub = next(a for a in build_parser()._actions if a.__class__.__name__ == "_SubParsersAction")
    missing = []
    for name, parser in sub.choices.items():
        if name != "resume" and f"`gatewright {name}" not in table:
            missing.append(name)
        for action in parser._actions:
            longest = max(action.option_strings, key=len, default="")
            if longest.startswith("--") and longest != "--help" and not any(f"{o}" in table for o in action.option_strings):
                missing.append(f"{name} {longest}")
    assert not missing, missing


def test_every_tui_key_is_documented():
    guide = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
    for b in GatewrightApp.BINDINGS:
        assert f"`{b.key}`" in guide, f"TUI key {b.key!r} ({b.description}) not in getting-started.md"


def github_slug(heading: str) -> str:
    """GitHub's anchor rules: lowercase, drop punctuation (keep - and _), spaces -> -."""
    s = heading.strip().lower()
    s = re.sub(r"[^\w\- ]", "", s, flags=re.UNICODE)
    return s.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    out, in_code = set(), False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code and line.startswith("#"):
            out.add(github_slug(line.lstrip("#")))
    return out


LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_links_resolve(doc):
    text = re.sub(r"```.*?```", "", doc.read_text(encoding="utf-8"), flags=re.S)
    problems = []
    for target in LINK.findall(text):
        if re.match(r"^[a-z]+://", target) or target.startswith("mailto:"):
            continue
        file_part, _, anchor = target.partition("#")
        path = (doc.parent / file_part).resolve() if file_part else doc
        if not path.exists():
            problems.append(f"missing file: {target}")
        elif anchor and path.suffix == ".md" and anchor not in anchors(path):
            problems.append(f"missing anchor: {target}")
    assert not problems, problems
