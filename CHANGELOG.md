# Changelog

All notable changes to Gatewright. The format follows [Keep a Changelog](https://keepachangelog.com/),
and versions follow [Semantic Versioning](https://semver.org/) (before 1.0, minor versions may change
config or behaviour).

## [Unreleased]

### License
- PolyForm Noncommercial License 1.0.0: free for noncommercial use, companies need permission (COMMERCIAL.md).

### Changed
- The self-pipeline enforces 100% coverage on Linux, also when Gatewright runs itself.

## [0.7.1] - 2026-11-23

Tested against real GitHub: private repository, approvals, statuses, comments, live webhooks through GitHub's relay and
on a public address, a pull request from a real fork, and polling of a very large repository.

### Changed
- Watched branches with plain names are asked for one by one. One poll of a repository with 6,000 branches and 3,200
  open pull requests took more than 95 requests for `branches: [main]`; it is 34 now (the pull requests).
- A rate limited host is left alone until the time it names.

### Added
- README: execution in the TUI and the CLI with screenshots of real runs, and every command.

### Fixed
- Webhooks are answered at once and the statuses posted afterwards: several calls to the host came first, and GitHub
  gives up after 10 seconds. The answer has a `Content-Length`, so proxies don't wait.
- The full text of every open pull request was stored on every poll.
- TUI: the Status and Note columns were cut off (`BLOCKE`).
- `install.sh --os-release` (testing) was ignored on macOS. Tests no longer depend on a display or the system code page.

## [0.7.0] - 2026-11-23

### Decision: should builds be separated?
- **Builds that are tested: yes, separated.** Every local build has its own history, cache, reports and artifacts, outside
  its folder and apart from every project. Sharing them would compare a run with another program's run, reuse another
  program's gate results, and let `promote last` deploy the wrong build. For the same reason a local build can't use a
  project's secrets, signing key, deploy targets or notifications; a folder opened with `own` is a project itself.
  Archives are unpacked to a copy. Folders are tested in place, not copied: a copy would lose the folder's virtual
  environment and double its disk use. The cost: tools leave their usual files there (`__pycache__`, `.coverage`, ...).
- **Gatewright itself for Windows and Linux: no, one build.** Wheels built on Windows and on Linux contain identical code
  (42 of 42 files), and the code that only runs on one OS is a handful of functions (`winsupport.py`, `locks.py`,
  `picker.py`). Two builds would mean two things to test and release that can drift apart. Only the installers are
  separate (`install.sh`, `install.ps1`). Revisit this if Gatewright ever ships compiled code or a bundled Python.
- Details: docs/architecture.md, "Local builds".

### Added
- docs/operating.md (how to operate Gatewright) and docs/project.md (why, timeline, decisions, roadmap). What was
  planned for 0.7 and is not in it moves to 0.8, and 0.8 to 0.9.
- **Local builds, without a git host**: `o` in the TUI opens a folder, archive or file on this computer, by full path,
  folder list, or the desktop's dialog (Explorer on Windows and WSL, zenity/kdialog/yad on Linux, Finder on macOS).
  `gatewright tui` outside a project asks at once. Also `gatewright run|plan --local PATH` and `gatewright tui --local`.
- `install.ps1`: installs, upgrades and uninstalls Gatewright on Windows (W17).
- `gatewright doctor` shows the Windows long path setting (W8).
- `gatewright clean` and `keep_runs:` delete old runs, never deployed ones (L9).
- `vcs.untrusted: containers | refuse` for approved but untrusted commits (L1), `vcs.parallel` (L7), `vcs.fetch_depth`,
  `vcs.submodules`, `vcs.lfs` (L8), `container_user: image` for jobs (L14).
- `gatewright keygen --passphrase`; signing through ssh-agent (L3).
- `install.sh --offline DIR`, `install.ps1 -Offline DIR` (L16).
- The TUI opens on the Queue tab, with the oldest commit highlighted, when commits wait for approval.

### Security
- **A job passed when its tool exited 0 but its report was missing or damaged**, e.g. a security scan. It is an error now.
- **Jobs inherited `GATEWRIGHT_SECRET_*`** from Gatewright's environment, also jobs of untrusted commits. They get only
  what their `secrets:` lists; untrusted ones also don't see git host tokens and the ssh-agent.
- **An untrusted commit could suppress its own findings** with its `baseline.yml`.
- Escape sequences printed by tested code reached the terminal (clipboard, window title, clear screen).
- Reports and artifacts no longer contain the secrets that a secret scan found (code snippets, gitleaks output).
- Secrets are also masked in base64, hex, URL and JSON encoding, and line by line (L2).
- The git token is sent to its own host only, not to submodule or LFS servers elsewhere.
- Windows: a warning when `.gatewright/secrets.env` is readable by other users, with the fix (W10).

### Changed
- Deployments to one environment wait for nobody: a second one at the same time is refused (L4).
- Identical flagged lines in one file are separate findings; a baseline entry covers the first (L11).
- Temporary notification failures are retried; ntfy titles keep non-ASCII characters (L15).
- The Python template runs the tests once (L17), and `init` also ignores deployments/ and workspaces/ in git.
- Windows: without a project venv, only `python`/`python3` are Gatewright's. Your own `pip`, `ruff`, `pytest`, ...
  now come before Gatewright's (W4).
- Windows: a stopped job first gets Ctrl+Break and 3 seconds to clean up (W6).

### Fixed
- `gatewright init` in a folder called e.g. `a: b` or `yes` wrote a broken pipeline, and an invalid image tag for names
  with spaces or capitals.
- In a repository without commits the commit was recorded as `HEAD`. "Dirty" now means this folder, not the whole
  enclosing repository.
- Paths with `[brackets]` in messages could be lost or break the output.
- The secret scan kept failing after a leaked key was removed: it found the key in Gatewright's own old reports.
  Existing projects: add `--redact --exit-code 0` to the gitleaks command, as `gatewright init` now does.
- Container jobs could not write to `$GATEWRIGHT_OUT` in watcher runs.
- Newer Docker CLIs: a stopped daemon was reported as a missing command.
- Container jobs on rootless Podman could not write to the project.
- GitLab: an error was logged when a commit was queued as a branch and as a merge request.
- The watcher missed branches and pull requests after the first page (L5) and kept closed ones (L6).
- Windows: `gatewright run`, `watch`, ... crashed when their output went to a file or a pipe (no `▶` or `✓` in cp1252).
- Windows: artifacts and watcher checkouts with paths over 260 characters (W8).
- Windows: the same file spelled with different case was two findings (W9).
- Windows: tools that Git Bash provides (`sleep`, `cat`, `grep`, ...) were reported as missing.
- Windows: watcher workspaces were never removed, and re-running the same commit crashed on them.
- Windows: copied outputs were listed with `\` instead of `/`.

## [0.6.2] - 2026-11-22

### Added
- **Your own coverage minimums**, per gate and for the whole run: `coverage: 80` on a gate or at the top
  of `pipeline.yml`, or `coverage: {min: 80, missing: fail|ignore}`. Gatewright sets no default:
  how much coverage a project needs is its owner's decision. A minimum fails its gate (respecting the
  gate's mode) or the run, and by default also fails when **nothing measured coverage**, so it can't
  pass silently. Partial runs (`-j`, `-c`, a subset of gates, e.g. the pre-push hook) skip minimums
  they have no measurement for.
- `gatewright run|plan --coverage 85` (whole run) and `--coverage test=90` (one gate), repeatable;
  kept by `rerun-failed`.
- `gatewright plan` and the TUI dry run show each minimum, and warn when a gate has a minimum but
  none of its jobs measures coverage.

### Changed
- When several jobs report `coverage.percent`, the **lowest** counts (was: whichever finished last).
  Release notes use the same rule.
- The Python template sets `coverage: 70` on its test gate (change it to what your project needs)
  instead of a `policy:` rule, and its coverage job is no longer optional.
- Gatewright's own pipeline uses `coverage: 100` on its test gate.

### Fixed
- An invalid `--gates`, `-j` or `--coverage` left an empty folder in `.gatewright/runs/`.

## [0.6.1] - 2026-11-22

A full bug review of every module, plus tests for **100% of lines and branches**, enforced by
Gatewright's own pipeline and CI. Every fix below has a regression test.

### Security
- Secret values inside **report files** (JUnit, SARIF, ...), other text files in `$GATEWRIGHT_OUT` and
  files copied by `outputs:` were not masked, so they could reach the history, the reports and the
  artifact. They are now masked like console output, before the checksums are written.
- A secret whose value changed during a run was masked only with its newest value.
- `gatewright rollback ENV --to RUN` skipped every promotion check, so it could deploy a run that never
  passed. A rollback target must now have been successfully live in that environment before.
- The watcher only accepts real commit ids from the git host. A value like `--upload-pack=...` would
  have reached `git fetch` as an option.
- `gatewright hook install -g ...` checks gate names before writing them into the shell hook.
- `gatewright release-notes --from/--to` refuses refs that start with `-` (git would read them as options).

### Fixed
- **Cancelling**: a cancelled run has its own status `cancelled` (exit code 130) and can't be promoted.
  Cancelled gates are reported to the git host as an error ("cancelled before it finished"), not as a
  green "skipped". A gate cancelled half-way is no longer PASSED. A job stopped by the user is
  "cancelled", not "stopped to protect the machine".
- A selection that matches no jobs is `skipped`, not `passed`.
- A failing test whose output mentions e.g. "Permission denied" was reported as an infrastructure
  error. Structured results (JUnit etc.) now win over guesses from words in the output.
- A report file left over from an earlier run was read when the tool crashed before writing a new one.
- Ctrl+C / watchdog stops during the retry wait, or before the process started, were ignored.
- Job output is capped at 64 MiB per stream in memory (the full output stays in the log file).
- `run:` starting with a quoted program path (`"C:\Program Files\..."`, `"/opt/my tools/x"`) was
  reported as "command not found".
- Docker: long job ids cut the attempt number off the container name, so retries could collide; a
  hung `docker kill` could crash the scheduler.
- JUnit `line` numbers were off by one; `line="0"` became line 1.
- mypy errors on Windows drive paths (`C:\...`) were dropped; a SARIF rule with `security-severity: null`
  dropped the whole report.
- `nvidia-smi` printing `[N/A]` for GPU memory crashed hardware detection; a non-UTF-8
  `/etc/os-release` crashed OS detection; FreeBSD was reported as Linux.
- Config: a non-UTF-8 job file, a `pipeline.yml` that isn't a mapping, or an empty job file crashed loading.
- `baseline.yml` that is broken or has wrong types (e.g. `expires: 90`) crashed runs; now a warning.
  `gatewright baseline list` showed "No suppressions" and hid that warning.
- Corrupt cache entries crashed runs; they are now cache misses.
- `secrets.env`: `export KEY=value` lines work; only matching quotes are stripped.
- Artifacts: output globs pointing outside the project (`../x`, absolute) crashed; malformed or
  escaping lines in `checksums.sha256` crashed `verify`.
- `gatewright report` wrote into the artifact folder and broke its checksums and signature. Reports go
  to `.gatewright/reports/<run>` (or `-o`).
- `gatewright history/queue/show/...` outside a project created a `.gatewright/` database there; they
  now say "No Gatewright project here".
- `gatewright run -v` printed literal `[dim]...[/]` markup; the Ctrl+C handler stayed installed after
  `run`/`watch` returned; `release-notes --run X` with an unknown run silently left out the quality section.
- `gatewright doctor`: a tool used by an optional and a required job could be shown as optional;
  shell builtins and `VAR=value` prefixes were listed as tools to install.
- Webhook server: a negative or non-numeric `Content-Length` blocked or crashed the handler (now 400).
- Matrix messages sent within the same millisecond were dropped as duplicates.
- Notification events `run_cancelled` and `run_skipped` can now be subscribed to.
- TUI: quitting during a run left the run going (and its result crashed the closing UI); late timer and
  table events while closing raised errors. The TUI also overrode Textual's internal `App._log`.
- The Python shims temp folder is removed at exit (known issue L9); tool versions are re-checked after
  5 minutes instead of cached forever.

### Changed
- Gatewright's own pipeline runs the test suite once, under coverage, and **requires 100% line and
  branch coverage** (policy `coverage >= 100` plus `tests-ran`). The coverage job is no longer optional.
- `pyproject.toml` holds the coverage settings (`branch = true`, `fail_under = 100`); CI enforces them
  on Linux. `coverage` is part of the `dev` extra.
- docs/testing.md: the coverage rules and the full list of `# pragma: no cover` exclusions.

## [0.6.0] - 2026-11-22

### Added
- **Artifact signing** with SSH signatures (`ssh-keygen -Y`, OpenSSH 8.2+): `signing:` config,
  `gatewright keygen`, automatic signing of every run, `gatewright sign`, and `gatewright verify` checks
  signatures, telling apart "changed after signing", "untrusted signer" and "edited signer name".
- **Environments**: `environments:` with `deploy`, `verify`, `requires`, `after`, `require_signature`,
  `approval`, `auto_rollback`, `release_notes`. Commands `gatewright promote`, `gatewright rollback`,
  `gatewright deployments`. Deployment history and per-deployment logs.
- **Release notes**: `gatewright release-notes` (Conventional Commits grouping, breaking changes,
  contributors, quality summary from a run).
- **Notifications**: ntfy, webhook (HMAC-signed, Slack/Mattermost/Discord-compatible), e-mail (SMTP),
  Matrix. `notify:` config with per-channel events, `gatewright notify-test`.
- `when_exists:` job option: run a job only when one of the listed files exists.
- `gatewright doctor` checks OpenSSH version, signing key, allowed signers, git version, VCS token and webhook secret.
- Docs: git-integration.md, releases.md, notifications.md, tips.md; known issues as a map.

### Changed
- Config files are read with YAML 1.2 booleans: only `true`/`false` are booleans (`on:` keys now work;
  `yes`/`no` must become `true`/`false`).
- The Python template's `deps-audit` job only runs when `requirements.txt` exists.

### Fixed
- `notify: on: [...]` was read as a boolean key by the YAML 1.1 rules.
- The Python template failed the commit gate in projects without `requirements.txt`.

## [0.5.0] - 2026-11-22

### Added
- **Git integration**: `vcs:` config for GitHub (incl. Enterprise), Gitea, Forgejo (incl. Codeberg) and
  GitLab. `gatewright watch` polls with ETags and/or receives signed webhooks.
- **Approval queue** (`gatewright queue`, `approve`, `reject`, TUI Queue tab with keys `u`, `a`, `n`):
  every item pinned to one commit SHA; newer commits supersede older pending ones.
- **Trust rules**: `mode: manual | auto-trusted | auto`, `trusted:` authors. Pull requests from forks
  are never auto-approved, run without secrets, and use your local job definitions.
- **Commit statuses per gate** (`gatewright/<gate>`) and pull request summary comments.
- **Pre-push hook**: `gatewright hook install | uninstall | status`.
- Engine can run code from a workspace while keeping history, cache and secrets in your project.

## [0.4.2] - 2026-11-22

### Changed
- Project named **Gatewright Pipeline**: command `gatewright`, project folder `.gatewright/`, variables
  `GATEWRIGHT_OUT`, `GATEWRIGHT_RUN_ID`, `GATEWRIGHT_JOB`, `GATEWRIGHT_GATE`, `GATEWRIGHT_SECRET_<NAME>`,
  PyPI name `gatewright-pipeline`.
- All reports are titled "Gatewright Pipeline report".

## [0.4.1] - 2026-11-22

### Added
- `install.sh`: distro-aware installer for Linux and macOS (Debian/Kali/Ubuntu, Fedora/RHEL, Arch,
  openSUSE, Alpine, Void, Gentoo, NixOS, Homebrew). Installs into its own venv (PEP 668-safe),
  installs the starter scanners, links `gatewright`, verifies the install, supports `--dry-run` and `--uninstall`.
- OS/distribution detection (`platform_info.py`). `gatewright doctor` gives install commands for your
  package manager. Run manifests record the distribution.
- Jobs use the project's own venv (`.venv`, `venv`, `env`) when present, else Gatewright's Python.
  `gatewright doctor` shows which.
- `shell:` option (pipeline or job): `auto`, `sh`, `bash`, `cmd`, `powershell`. On Windows `auto`
  prefers Git Bash and skips WSL bash.
- `$GATEWRIGHT_OUT`, `%GATEWRIGHT_OUT%`, `$env:GATEWRIGHT_OUT` and `${GATEWRIGHT_OUT}` work in every shell.
- Container jobs: paths are mapped back to the project, run as the host user on Linux, unique names
  per retry, clear errors for a missing image or a stopped daemon.
- Documentation set in `docs/`, module and function docstrings, contribution files, issue templates,
  optional CI workflow.
- Tests: real Docker tests, installer tests for 23 distributions, Windows behaviour simulations, TUI tests.

### Changed
- Configuration is validated by a built-in validator instead of `jsonschema` (no Rust/compiled dependency).
- Timeout, cancel and the memory watchdog stop the whole process tree on every OS.
- All files are read and written as UTF-8. Python tools in jobs print UTF-8.
- git is optional in `gatewright doctor`.

### Fixed
- Jobs like `python -m pytest` used a Python without the installed tools.
- Run history could order two runs from the same second wrongly (affected `rerun-failed` and `diff`).
- Windows: cmd built-ins / PowerShell cmdlets were reported as missing tools, error locations with
  drive letters weren't recognised, and fingerprints differed between `\` and `/` paths.

## [0.4.0] - 2026-11-22

First version: engine, gates (enforced/advisory), resource-aware scheduler with memory watchdog,
structured human-readable errors with code snippets, parsers (ruff, bandit, junit, sarif, gitleaks,
pip-audit, mypy, coverage), caching, history and diffs, rerun-failed, secrets masking, baselines,
policy as code, Markdown/HTML/PDF/SARIF/JUnit reports, artifact bundle with checksums, `doctor`,
`init` templates, and the Textual terminal UI.
