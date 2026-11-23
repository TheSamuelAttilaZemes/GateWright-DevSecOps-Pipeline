# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""`gatewright init` starter pipelines (python, node, docker).

Each template is three strings/dicts: the gates block, the policy block and a
{filename: yaml} dict of job files written to .gatewright/jobs/. `detect_template`
picks one from files in the project (package.json -> node, ...).

To add a template: add GATES/POLICY/JOBS constants, register them in TEMPLATES,
extend `detect_template`, and add the name to the CLI `--template` choices.
tests/test_config.py loads every template, so a broken one fails the tests.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

GITIGNORE = [".gatewright/runs/", ".gatewright/cache/", ".gatewright/deployments/", ".gatewright/workspaces/",
             ".gatewright/gatewright.db", ".gatewright/secrets.env"]

COMMON_PIPELINE = """\
# Gatewright pipeline. Gates run in this order. `mode: enforced` stops the pipeline
# on failure; `mode: advisory` records the failure and continues.
version: 1
name: {name}
profile: auto          # auto | laptop | server
limits:
  kill_mem_percent: 92 # watchdog stops the newest job above this RAM usage
gates:
{gates}
policy:
{policy}

# --- Optional (remove the # to use). Guides in docs/: git-integration.md, releases.md, notifications.md
# vcs:                                  # follow your git host, approvals, statuses per gate
#   provider: github                    # github | gitea | forgejo | gitlab
#   repo: you/{plain}
#   mode: manual                        # manual | auto-trusted | auto
#   trusted: [you]
# signing:                              # sign every artifact (create a key: gatewright keygen)
#   key: ~/.config/gatewright/signing_key
#   identity: you@example.com
# environments:                         # gatewright promote --to staging
#   staging:
#     deploy: python3 scripts/deploy.py staging
#     verify: python3 scripts/health.py staging
#     requires: [test]
#     auto_rollback: true
# notify:                               # gatewright notify-test
#   channels:
#     - type: ntfy
#       url: https://ntfy.sh/your-long-secret-topic
"""

PY_GATES = """\
  - id: commit
    mode: enforced
    description: Fast checks on every commit (lint, secrets, SAST, dependencies)
  - id: build
    mode: enforced
    description: The code compiles / packages
  - id: test
    mode: enforced
    description: Unit tests and coverage
    coverage: 70       # minimum test coverage for this gate: set what your project needs (0-100)
  - id: staging
    mode: advisory
    enabled: false     # opt in with `gatewright run --gates staging`
    requires: [test]   # test must have passed for this commit
    description: Deploy to staging and smoke test"""

PY_POLICY = """\
  - id: no-critical
    metric: findings.critical
    op: "=="
    value: 0
    description: No critical findings (e.g. leaked secrets) anywhere.
  - id: no-high-in-commit
    gates: [commit]
    metric: findings.high
    op: "=="
    value: 0
    description: High severity security findings block the commit gate."""

PY_JOBS = {
    "commit.yml": """\
jobs:
  - id: lint
    name: Ruff lint
    gate: commit
    category: dev
    run: ruff check --output-format json .
    parser: ruff
    tool: ruff
    fail_on: medium          # style issues (low) warn, real bugs fail
    inputs: ["**/*.py", "pyproject.toml", "ruff.toml"]
    resources: {cpu: 1, mem: 256MB}
    timeout: 120

  - id: format
    name: Formatting
    gate: commit
    category: dev
    run: ruff format --check --diff .
    tool: ruff
    fail_on: high
    inputs: ["**/*.py"]
    timeout: 120

  - id: secrets-scan
    name: Secret scan (gitleaks)
    gate: commit
    category: sec
    run: gitleaks detect --no-git --source . --report-format json --report-path "$GATEWRIGHT_OUT/gitleaks.json" --redact --exit-code 0
    parser: {type: gitleaks, file: "$GATEWRIGHT_OUT/gitleaks.json"}
    tool: gitleaks
    optional: true           # skipped (not failed) if gitleaks is not installed
    cache: false

  - id: sast
    name: SAST (bandit)
    gate: commit
    category: sec
    run: bandit -r . -f json -q -x ./.venv,./venv,./tests,./.gatewright
    parser: bandit
    tool: bandit
    inputs: ["**/*.py"]
    resources: {cpu: 1, mem: 512MB}

  - id: deps-audit
    name: Dependency vulnerabilities (pip-audit)
    gate: commit
    category: sec
    run: pip-audit -r requirements.txt -f json
    parser: pip-audit
    tool: pip-audit
    optional: true
    when_exists: [requirements.txt]   # nothing to audit without it
    retries: 2               # network errors are retried
    inputs: ["requirements*.txt", "pyproject.toml"]
""",
    "build.yml": """\
jobs:
  - id: compile
    name: Byte-compile all modules
    gate: build
    category: dev
    run: python -m compileall -q -x "(\\.venv|\\.gatewright|\\.git)" .
    inputs: ["**/*.py"]
""",
    "test.yml": """\
jobs:
  - id: unit-tests
    name: Unit tests (pytest)
    gate: test
    category: dev
    needs: [compile]
    run: python -m coverage run -m pytest -q -p no:cacheprovider --junitxml="$GATEWRIGHT_OUT/junit.xml"
    parser: {type: junit, file: "$GATEWRIGHT_OUT/junit.xml"}
    tool: pytest
    resources: {cpu: 2, mem: 1GB}
    timeout: 900
    cache: false             # the coverage job needs the data this run writes

  - id: coverage
    name: Test coverage
    gate: test
    category: dev
    needs: [unit-tests]
    run: python -m coverage json -o "$GATEWRIGHT_OUT/coverage.json"
    parser: {type: coverage, file: "$GATEWRIGHT_OUT/coverage.json"}
    tool: coverage     # required: the test gate's `coverage:` minimum needs this measurement
    cache: false
""",
    "staging.yml": """\
jobs:
  - id: smoke
    name: Smoke test (replace with your deploy + health check)
    gate: staging
    category: ops
    run: python -c "print('deploy to staging, then e.g. curl -f https://staging.example/health')"
    cache: false
""",
}

NODE_GATES = """\
  - id: commit
    mode: enforced
  - id: build
    mode: enforced
  - id: test
    mode: enforced"""

NODE_POLICY = """\
  - id: no-critical
    metric: findings.critical
    op: "=="
    value: 0"""

NODE_JOBS = {
    "commit.yml": """\
jobs:
  - id: install
    gate: commit
    category: dev
    run: npm ci --no-audit --no-fund
    tool: npm
    retries: 2
  - id: lint
    gate: commit
    category: dev
    needs: [install]
    run: npx eslint .
    tool: npx
  - id: deps-audit
    gate: commit
    category: sec
    needs: [install]
    run: npm audit --audit-level=high
    tool: npm
    retries: 1
  - id: secrets-scan
    gate: commit
    category: sec
    run: gitleaks detect --no-git --source . --report-format json --report-path "$GATEWRIGHT_OUT/gitleaks.json" --redact --exit-code 0
    parser: {type: gitleaks, file: "$GATEWRIGHT_OUT/gitleaks.json"}
    tool: gitleaks
    optional: true
    cache: false
""",
    "build.yml": """\
jobs:
  - id: build
    gate: build
    category: dev
    run: npm run build --if-present
    tool: npm
    outputs: ["dist/**"]
""",
    "test.yml": """\
jobs:
  - id: unit-tests
    gate: test
    category: dev
    run: npm test --if-present
    tool: npm
    resources: {cpu: 2, mem: 1GB}
""",
}

DOCKER_GATES = """\
  - id: commit
    mode: enforced
  - id: build
    mode: enforced
  - id: scan
    mode: enforced"""

DOCKER_POLICY = """\
  - id: no-critical-cves
    metric: findings.critical
    op: "=="
    value: 0"""

DOCKER_JOBS = {
    "commit.yml": """\
jobs:
  - id: dockerfile-lint
    gate: commit
    category: dev
    run: hadolint -f sarif Dockerfile
    parser: sarif
    tool: hadolint
    optional: true
  - id: secrets-scan
    gate: commit
    category: sec
    run: gitleaks detect --no-git --source . --report-format json --report-path "$GATEWRIGHT_OUT/gitleaks.json" --redact --exit-code 0
    parser: {type: gitleaks, file: "$GATEWRIGHT_OUT/gitleaks.json"}
    tool: gitleaks
    optional: true
    cache: false
""",
    "build.yml": """\
jobs:
  - id: image
    gate: build
    category: ops
    run: docker build -t "gatewright-local/{name}:$GATEWRIGHT_RUN_ID" .
    tool: docker
    resources: {cpu: 2, mem: 2GB}
    timeout: 1800
    cache: false
""",
    "scan.yml": """\
jobs:
  - id: image-scan
    gate: scan
    category: sec
    run: trivy image --format sarif --output "$GATEWRIGHT_OUT/trivy.sarif" "gatewright-local/{name}:$GATEWRIGHT_RUN_ID"
    parser: {type: sarif, file: "$GATEWRIGHT_OUT/trivy.sarif"}
    tool: trivy
    retries: 2
    cache: false
""",
}

TEMPLATES = {
    "python": (PY_GATES, PY_POLICY, PY_JOBS),
    "node": (NODE_GATES, NODE_POLICY, NODE_JOBS),
    "docker": (DOCKER_GATES, DOCKER_POLICY, DOCKER_JOBS),
}


def detect_template(root: Path) -> str:
    """Guess the template from the project's files."""
    if (root / "package.json").exists():
        return "node"
    if any((root / f).exists() for f in ("pyproject.toml", "setup.py", "requirements.txt")) or \
            any(root.glob("*.py")):
        return "python"
    if (root / "Dockerfile").exists():
        return "docker"
    return "python"


def write(root: Path, template: str, name: str) -> list[str]:
    """Write a template's pipeline.yml and job files below `root`. Returns the files written."""
    gates, policy, jobs = TEMPLATES[template]
    fdir = root / ".gatewright"
    (fdir / "jobs").mkdir(parents=True, exist_ok=True)
    p = fdir / "pipeline.yml"
    # a folder can be called anything: quote it for YAML, reduce it for the repo example and image tags
    plain = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "project"
    p.write_text(COMMON_PIPELINE.format(name=json.dumps(name, ensure_ascii=True), plain=plain, gates=gates,
                                        policy=policy), encoding="utf-8")
    written = [str(p.relative_to(root))]
    tag = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"
    for fname, content in jobs.items():
        jp = fdir / "jobs" / fname
        jp.write_text(content.replace("{name}", tag), encoding="utf-8")
        written.append(str(jp.relative_to(root)))
    return written


def init(root: Path, template: str | None = None, force: bool = False) -> tuple[str, list[str]]:
    """Write .gatewright/pipeline.yml, .gatewright/jobs/*.yml and .gitignore entries.
    Returns (template used, files written). Refuses to overwrite unless `force`.
    """
    template = template or detect_template(root)
    if template not in TEMPLATES:
        raise ValueError(f"Unknown template `{template}`. Choose from: {', '.join(TEMPLATES)}")
    fdir = root / ".gatewright"
    if (fdir / "pipeline.yml").exists() and not force:
        raise FileExistsError(f"{fdir / 'pipeline.yml'} already exists (use --force to overwrite)")
    written = write(root, template, root.resolve().name)
    gi = root / ".gitignore"
    existing = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    add = [g for g in GITIGNORE if g not in existing]
    if add:
        with open(gi, "a", encoding="utf-8") as fh:
            fh.write(("\n" if existing and existing[-1] else "") + "# gatewright\n" + "\n".join(add) + "\n")
        written.append(".gitignore")
    return template, written
