# Contributing to Gatewright

Thanks for helping! Gatewright is meant to be taken apart and adapted, so contributions of any size are welcome.

## Good first contributions

- **Test reports from real systems.** Windows, macOS, Fedora, Arch, openSUSE, Kali... The biggest gaps
  are listed in [docs/known-issues.md](docs/known-issues.md). A report saying "installed and ran fine on X"
  is valuable too.
- **Friendlier explanations** for tool rules: one entry in `CATALOG` in `gatewright/errors.py`
  ([how](docs/extending.md#add-a-friendlier-explanation-for-a-rule)).
- **Install hints** for tools on your distribution (`TOOL_PACKAGES` in `gatewright/platform_info.py`).
- **Parsers** for more tools ([how](docs/extending.md#support-a-new-tool-parser)), tested with real output.
- **Real git host reports**: try `gatewright watch` against GitHub, Gitea, Forgejo or GitLab and tell us
  what worked ([known issue T2](docs/known-issues.md#t2-github-is-only-partly-tested-for-real)).
- **Git hosts and notification channels** ([how](docs/extending.md#add-a-git-host)).
- **Documentation**: anything that confused you is a bug in the docs.

## Development setup

```bash
git clone <your fork> gatewright && cd gatewright
python3 -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev,tools,pdf]"
pytest
gatewright run                                           # Gatewright's own pipeline on itself
```

## Before opening a pull request

- [ ] `pytest` passes (skipped tests are fine if their reason applies to your machine)
- [ ] `ruff check gatewright tests --select E,F,W --ignore E501` is clean
- [ ] `bandit -q -r gatewright -ll` reports nothing new
- [ ] `shellcheck -s sh install.sh` is clean (if you touched the installer)
- [ ] New behaviour has a test, ideally a real run rather than mocks ([docs/testing.md](docs/testing.md))
- [ ] Docs updated: options in `docs/configuration.md`, behaviour in the relevant page, and a line in `CHANGELOG.md`
- [ ] Platform-specific changes say where you tested them (OS, distribution, shell)

## Branches

`main` is the working, released source; `development` is for daily work.

1. Fork, branch from `development` (`feature/...`, `fix/...`) and open a pull request to it.
2. CI runs on Linux and Windows. A maintainer approves the first run of a new contributor.
3. `development` goes to `main` by pull request, when all of CI is green. Releases are tagged on `main`.

## Rules for every change

1. **100% line and branch coverage** of `gatewright/` on Linux. New code ships with tests.
2. Test both sides of anything that depends on the machine, with fakes where needed.
3. `# pragma: no cover` only for code Linux can't run, with a reason on the line and a row in
   [docs/testing.md](docs/testing.md#pragmas-only-for-code-the-test-os-cant-run).
4. Every bug fix gets a test whose docstring or comment says `Regression:` and what was wrong.
5. Errors are for humans: what went wrong, where, what to do. A bad config value never ends in a traceback.
6. Docs change with the code: `tests/test_docs.py` checks every config key, command, TUI key and link.
7. `CHANGELOG.md`, and the version in **both** `pyproject.toml` and `gatewright/__init__.py`.
8. [docs/known-issues.md](docs/known-issues.md) stays honest. It is handed out as a separate file with each release.
9. The [security decisions](docs/project.md#security-decisions-that-must-not-regress) stay.
10. The name is **Gatewright Pipeline**, a DevSecOps pipeline.

## Code guidelines

- **Logic lives in modules, not in `cli.py` or `tui.py`.** Both front-ends must get the same behaviour.
- **Results are data.** Return `JobError`/`JobResult` objects, not printed text.
- **Explain for humans.** Error texts say what's wrong, why it matters and what to do, in plain language.
- **Never fail silently.** If a tool's output can't be read, report it as a crash, never as "no findings".
- **Portable by default.** Use `pathlib`/`os.path`, `encoding="utf-8"` on every file read/write, no
  shell-specific syntax in Python, and POSIX `sh` (no bashisms) in `install.sh`.
- **No secrets on command lines or in logs.** Use the environment and `SecretStore.mask`.
- **Keep dependencies few and pure Python where possible**, so Gatewright installs on any distribution and
  CPU architecture without a compiler.
- Every module starts with a docstring explaining its role. Keep them current when you change things.

## Commit messages and pull requests

Short imperative subject ("Add shellcheck parser"), and a body explaining **why** when it isn't obvious.
One topic per pull request. Draft PRs are welcome for early feedback.

## Reporting bugs and security issues

Use the issue templates. **Security problems go privately** via [SECURITY.md](SECURITY.md), not into public issues.

## Code of conduct

Be kind and assume good intent. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Contributor agreement

Before your first contribution is accepted, read [CLA.md](CLA.md) and comment on your pull request:
`I have read the CLA (version 1) and agree to it.` You keep your copyright. Once is enough. The project's license is
the [PolyForm Noncommercial License 1.0.0](LICENSE.md): free for noncommercial use.
