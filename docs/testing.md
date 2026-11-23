# Testing Gatewright

```bash
pip install -e ".[dev,tools,pdf]"
pytest                      # everything that can run on this machine
pytest -k install           # just the installer tests
pytest tests/test_tui.py    # the terminal UI, driven headlessly
```

Every skipped test prints why it was skipped.

## 100% coverage: lines and branches

Gatewright's own tests cover **every line and every branch** of the `gatewright` package. This is
enforced, not just measured:

```bash
python -m coverage run -m pytest      # settings in pyproject.toml: branch = true, fail_under = 100
python -m coverage combine
python -m coverage report             # exits 2 and lists the missing lines if anything is untested
```

The same rule is enforced in three places:

1. **The self-pipeline** (`gatewright run` in this repo): `unit-tests` runs the suite once under coverage,
   `coverage` fails below 100%, and the test gate's `coverage: 100` setting blocks the gate.
   The `tests-ran` policy makes sure an empty test run can't pass.
2. **CI** on Linux / Python 3.12 (`.github/workflows/tests.yml`).
3. **`coverage report`** on your machine (the settings live in `pyproject.toml`).

Linux is the reference platform. Windows and macOS take other OS branches, so coverage is measured
there but not enforced.

The 100% holds with or without Docker, WeasyPrint, a colour terminal, a venv or root rights: code that
depends on the machine is tested with fakes too (a fake `docker` program, a fake `weasyprint` module,
both values of `NO_COLOR`, both uid 0 and a normal uid). If you add code like that, test both sides.
Check it like this: `GATEWRIGHT_TEST_IMAGE=none:0 python -m coverage run -m pytest` as a normal user
in a venv must still report 100%.

### Pragmas: only for code the test OS can't run

`# pragma: no cover` and `# pragma: no branch` are allowed only for code the test OS (Linux) can't run.
Every exclusion has a reason on the same line. The complete list:

| Where | Why it can't run in the Linux test suite |
|---|---|
| `runner.py` `execute`: `else:` → `creationflags` | Windows only: new process group instead of `start_new_session` |
| `runner.py` `tool_available`: `if shell == "bash" and os.name == "nt":` | Windows only: tools that Git Bash brings |
| `runner.py` `execute`: `if os.name == "nt" and nice:` | Windows only: below-normal priority (POSIX uses `os.nice`) |
| `secrets.py` `if sys.platform == "win32":` | Windows only: an access list instead of mode bits |
| `local.py` `state_dir` | Windows only: `%LOCALAPPDATA%` |
| `locks.py` `if sys.platform == "win32":` | Windows only: `msvcrt` locks (POSIX uses `fcntl`) |
| `winsupport.py` `long_path`, `long_paths_enabled`, `file_sddl` | Windows only: calls into the Windows API |
| `signing.py` `if os.name == "posix":` (`no branch`) | the "not POSIX" side is Windows: the key inherits the profile folder's ACL |
| `models.py` `fmt_bytes`: final `raise` | unreachable by construction (the loop always returns at TB) |
| `if __name__ == "__main__":` (pyproject `exclude_also`) | only runs as a script, not under the test runner |

A new pragma needs a reason in the code and a row here, and a reviewer should ask whether a test
(with a fake, if needed) would be better.

## The test files

| File | What it covers |
|---|---|
| `test_config.py` | Config loading, every validation message, templates, built-in validator vs. jsonschema (random configs) |
| `test_engine.py` | Real runs end to end: gates, enforced/advisory, dependencies, timeouts, missing tools, retries, secrets masking, cache, rerun-failed, baseline + policy, history/diff, gate `requires` (with git), selection, dry run, scheduler limits, memory watchdog, cancel, manifest |
| `test_parsers.py` | Every parser with sample output, infra classification, fingerprints, SARIF/JUnit/Markdown/HTML exports |
| `test_portability.py` | Windows behaviour simulated on Linux (shells, paths, process trees, encodings), project venv / PATH order |
| `test_local.py` | Local builds: paths, hostile and fuzzed archives, isolation, dialogs, CLI, the `o` window, hostile output |
| `test_known_issues.py` | One or more regression tests per fixed entry of known-issues.md |
| `test_winsupport.py` | Long paths, path case, file permissions, stopping jobs, python wrappers, `install.ps1`: decisions everywhere, real behaviour on Windows |
| `test_install.py` | `install.sh` dry runs against real `os-release` files of 23 distributions, shell compatibility, ShellCheck, Python/shell table sync |
| `test_docker.py` | Container jobs against a **real** Docker daemon |
| `test_tui.py` | The Textual app: dry run, run, auto-selected errors, mode toggle, re-run failed, Queue tab approve/reject |
| `test_watch.py` | The watcher end to end against GitHub, Gitea and GitLab APIs: statuses per gate, ETags, trust modes, malicious fork PR, SHA pinning, superseding, bad token, webhooks, CLI |
| `test_vcs.py` | Each host adapter: webhook signatures and payloads, clone URLs, PR refs, error messages |
| `test_hooks.py` | Pre-push hook install/backup/uninstall, and a real `git push` blocked by a failing gate |
| `test_signing.py` | Real `ssh-keygen`: sign, verify, edited file, rewritten checksums, untrusted signer, edited signer name, missing key |
| `test_deploy.py` | Promote through environments, `after`/`requires`/approval rules, tampered artifacts, verify + auto-rollback, manual rollback, CLI |
| `test_release_notify.py` | Release notes from real git history; ntfy/webhook/Matrix/e-mail delivered to local servers; failures as warnings |
| `test_cli.py` | Every `gatewright` command in-process: exit codes, messages, refusing to run outside a project |
| `test_doctor.py` | Each doctor check with the machine's answers faked (no Docker, no daemon, unknown OS, Windows, OpenSSH versions) |
| `test_units.py` | Small modules directly: artifacts, baseline, cache, secrets, graph, policy, history |
| `test_runner_edges.py` | Runner bugs found in review: stale reports, cancel during retry wait, capture limit, secret masking in report files, container names |
| `test_errors_edges.py` | Parsers on messy output: invalid XML/JSON, Windows paths, missing severities |
| `test_watch_edges.py` | Watcher, deploy and signing failure paths; cancelled gates never shown as passed |
| `test_vcs_notify_edges.py` | Host adapters and notification channels; invalid commit ids are rejected |
| `test_platform_hw.py` | OS and hardware detection on machines the CI doesn't have (macOS, Windows, FreeBSD, GPUs, batteries) |
| `test_tui_edges.py` | TUI: reopening, history, cancel, quitting during a run, teardown races |
| `test_misc_edges.py`, `test_final_edges.py`, `test_branches.py` | The remaining branches: config errors, scheduler admission, templates, render, reports, environment-independent paths |

## The fake git host

`tests/fakehost.py` is a small HTTP server that implements the parts of the GitHub, Gitea/Forgejo and
GitLab REST APIs Gatewright uses, following each host's documentation. It is backed by a **real bare git
repository**, so branches, PR refs, fetches and checkouts are real git. It records every status,
comment and request, so tests can assert on exactly what Gatewright sent.
It is not the real service: see [known-issues.md](known-issues.md) for what has not been tried against
real hosts yet.

## Docker tests

They need a running daemon and an image that has `python` and `sh`:

```bash
docker pull python:3.12-alpine                 # the default image
pytest tests/test_docker.py
GATEWRIGHT_TEST_IMAGE=my-registry/python:3.12 pytest tests/test_docker.py   # or your own image
```

They're skipped automatically when Docker or the image isn't available.

## Testing the installer on a real distribution

```bash
docker run --rm -it -v "$PWD":/src:ro fedora:41 sh -c '
  cp -r /src /tmp/gatewright && cd /tmp/gatewright && sh install.sh --yes &&
  ~/.local/bin/gatewright --version && ~/.local/bin/gatewright doctor'
```

Swap `fedora:41` for `archlinux`, `opensuse/tumbleweed`, `alpine`, `kalilinux/kali-rolling`, etc.
Mention the result in your pull request: these real-distribution runs are the gaps listed in
[known-issues.md](known-issues.md).

## Gatewright tests itself

The repository has its own `.gatewright/` pipeline: `gatewright run` in the repo lints, security-scans and
tests Gatewright with Gatewright, and enforces 100% coverage (see above). A run that drops a single
line of coverage fails the `test` gate and prints the missing lines in the `coverage` job log.

## CI

`.github/workflows/tests.yml` runs the suite on Linux (Python 3.10, 3.12 with the 100% rule, 3.13) and Windows, for every
pull request and for `main` and `development`. GitHub's own runners are free for public repositories. If you don't use
GitHub, delete the folder and run the same commands from any CI, or from Gatewright itself.

## Writing tests

- Build a throwaway project with the `project` fixture (`tests/conftest.py`): it takes `pipeline.yml`
  text, job YAML (`{PY}` becomes the current Python) and extra files.
- Job commands in POSIX syntax need `@needs_posix_shell`. Linux/macOS-only behaviour needs `@posix_only`.
- Prefer real runs over mocks. Gatewright is mostly about processes, files and tools behaving for real.
- For a new parser, use **captured real output** of the tool.
- Every bug fix gets a regression test, with `Regression:` in its docstring or comment saying what broke.
- Code that depends on the machine (Docker, a library, the OS, root) needs tests for both sides, with a
  fake where needed, so coverage stays 100% everywhere.
