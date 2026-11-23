# The project: why, how it grew, and what comes next

## Contents
- [Why Gatewright exists](#why-gatewright-exists)
- [What it has to do](#what-it-has-to-do)
- [Timeline](#timeline)
- [Design decisions](#design-decisions)
- [Security decisions that must not regress](#security-decisions-that-must-not-regress)
- [Roadmap](#roadmap)
- [Gotchas](#gotchas)

## Why Gatewright exists

Hosted pipelines cost credits, run on somebody else's machines and decide how your checks work.
Gatewright is a DevSecOps pipeline you own completely: written in Python, configured in YAML, operated
from a terminal, running on your own laptop or server. Nothing in it needs a cloud account.

## What it has to do

| Requirement | Where it lives |
|---|---|
| Python runs the tasks, YAML defines the pipeline | `config.py`, `.gatewright/pipeline.yml`, `.gatewright/jobs/` |
| Tests for **Dev, Sec and Ops**, from the first commit to production | job `category`, gates, `environments` |
| A terminal UI with jobs preloaded, tests at specific **gates**, an **artifact** from every run | `tui.py`, `engine.py`, `artifacts.py` |
| **Detect the hardware** and never bring the machine down, on a PC and on a server | `hardware.py`, `scheduler.py` |
| **Choose the gates** per run, each *enforced* (stops later gates) or *advisory* (only records) | `Selection`, gate `mode` |
| **Reports** as Markdown or PDF, and guidance on what to do with artifacts | `reports.py`, [errors-and-reports.md](errors-and-reports.md) |
| **Errors in human language**, the failing code and job marked in red | `errors.py`, `render.py` |
| Show ✅/❌ on commits, start on a change, but **wait for approval** | `watch.py`, `queue.py`, `vcs/` |
| Test a build on this computer **without a git host** | `local.py`, `picker.py`, `tui_open.py` |
| Install and run on **every Linux distribution**, and on Windows | `install.sh`, `install.ps1`, `platform_info.py` |
| Source-available, documented so that others can adapt it | `docs/`, docstrings |
| Gatewright's **own code 100% covered** by its tests; coverage of your project is **your choice** | `pyproject.toml`, `coverage:` |

## Timeline

Dates are from the [changelog](../CHANGELOG.md). Planning and the versions before 0.4.0 have no more exact record than
"on or before 2026-11-22".

| Version | What happened |
|---|---|
| Planning | Which Dev/Sec/Ops tests a pipeline needs from testing to production. Design: terminal UI, gates, artifacts, hardware detection, enforced/advisory gates, readable errors |
| 0.1 to 0.4.0 | Engine, gates, scheduler with memory watchdog, runner, parsers, reports, TUI, caching, history, baselines, policy, doctor, templates, secrets, dry run, resume |
| 0.4.1 | Distro-aware `install.sh`, OS detection, shell selection, container path mapping, the docs, CI |
| 0.4.2 | Renamed to **Gatewright Pipeline** (command `gatewright`, folder `.gatewright/`, `GATEWRIGHT_*`) |
| 0.5.0 | Git integration: GitHub, Gitea, Forgejo, GitLab; approval queue; trust rules; a status per gate; pre-push hook |
| 0.6.0 | Signing, environments with promote/verify/rollback, release notes, notifications |
| 0.6.1 | Bug review of every module, about 40 fixes, 100% of lines and branches covered |
| 0.6.2 | Coverage minimums per gate and per run, chosen by each project |
| 0.7.0 | First run on real Windows 11; Linux in WSL, real Docker, nine real distributions, real GitHub, Gitea, Forgejo and GitLab; local builds; run cleanup, deployment lock, ssh-agent signing; security fixes |
| 0.7.1 | Real GitHub: webhooks, a fork, a very large repository; rate limits; first tagged release |

## Design decisions

- **One engine, many front-ends.** The engine reports through events; CLI, TUI and watcher only listen. They can't
  behave differently.
- **A built-in schema validator** instead of jsonschema: no compiled dependency on unusual distributions.
- **YAML 1.2 booleans** (only `true`/`false`): `on:` in a config was being read as `True`.
- **Hardware budget:** laptop or server profile from CPU count and battery. A job larger than the budget runs alone, so
  nothing waits forever.
- **CODE or INFRA errors:** telling "your code is wrong" from "the machine or the tool is wrong" is the core of readable
  errors. Structured results (e.g. JUnit) beat guesses from words in the output.
- **Cancelled is its own status:** never green on the git host, never promotable.
- **Coverage minimums have no default**, a missing measurement fails, the lowest of several reports counts, partial
  runs skip the check, and a gate with a minimum but no jobs is a config error.
- **Signing uses SSH keys**, not GPG: most developers already have `ssh-keygen`.
- **Reports made later** go to `.gatewright/reports/<run>`, never into the artifact: that would break its signature.
- **Tested builds are kept apart; Gatewright itself is one build** for all operating systems: see
  [architecture.md](architecture.md#local-builds) and the decision in the [changelog](../CHANGELOG.md).

## Security decisions that must not regress

- Pushes are trusted; pull requests from forks are never approved automatically, run without secrets and with *your*
  job definitions. Every queue item is pinned to one exact commit, and only valid commit ids reach git.
- The token reaches git through the environment, never through arguments, and only for its own host.
- Secrets are masked in the console, logs, report files, everything in `$GATEWRIGHT_OUT` and files copied by `outputs:`,
  also in common encodings. A changed secret stays masked.
- Jobs get only the secrets they list. Untrusted code sees no Gatewright secret and can't use its own `baseline.yml`.
- A missing or damaged report is an error, never a pass.
- Reports and artifacts don't contain the secret that a scan found.
- Output of tested code can't control the terminal.
- `rollback --to` accepts only runs that were live in that environment; one deployment per environment at a time.
- Archives can't write outside their folder, create links, or unpack without limit.
- Gate names in hooks and refs in release notes are validated; webhooks need a valid signature and length.

## Roadmap

| Version | Planned |
|---|---|
| 0.8 | Scheduled runs, multi-project support, remote agents over SSH |
| 0.9 | Plugin system, trend dashboard, inline pull request annotations (Checks API) |
| Open | macOS for real, editing coverage minimums and baselines in the TUI |

Run cleanup, deployment locking and ssh-agent signing were planned for these versions and are done in 0.7.0.

## Gotchas

- git **2.31+** for the watcher (token through `GIT_CONFIG_COUNT`).
- OpenSSH **8.2+** for signing; **8.7** can crash in `find-principals` (`gatewright doctor` warns).
- PDF needs WeasyPrint **and** the Pango system library. Without it, PDF is skipped with a warning.
- `coverage json` also applies `fail_under`; the self-pipeline passes `--fail-under=0` to it, so that
  `coverage report` still prints the missing lines.
- Rich wraps long lines at the terminal width; CLI tests set a wide console.
- `shell: powershell` reports every failure as exit code 1; add `; exit $LASTEXITCODE` for the real one.
- Newer Docker CLIs exit 127 when the daemon is down.
- Timing-sensitive tests (cancel, scheduler, TUI) must also pass on a busy machine; TUI teardown is guarded by
  `_while_mounted`.
