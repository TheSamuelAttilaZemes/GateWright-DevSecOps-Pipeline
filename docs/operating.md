# Operating Gatewright

A walk through daily use, from installing to reading results. Every option is in
[configuration.md](configuration.md); this page is about what to *do*.

## Contents
- [The idea in one minute](#the-idea-in-one-minute)
- [1. Install](#1-install)
- [2. Set up a project](#2-set-up-a-project)
- [3. Run it](#3-run-it)
- [4. Read the result](#4-read-the-result)
- [5. Test a build on your computer](#5-test-a-build-on-your-computer)
- [6. Let it follow your git host](#6-let-it-follow-your-git-host)
- [7. Release, deploy, roll back](#7-release-deploy-roll-back)
- [8. Keep it healthy](#8-keep-it-healthy)
- [When something goes wrong](#when-something-goes-wrong)

## The idea in one minute

```
 your code ──▶ gate: commit ──▶ gate: build ──▶ gate: test ──▶ artifact ──▶ deploy
                dev sec ops       dev sec ops     dev sec ops    reports, checksums, signature
```

- A **job** is one command: a linter, a scanner, the tests. Its category is `dev`, `sec` or `ops`.
- A **gate** is a group of jobs. *Enforced*: if it fails, later gates are blocked. *Advisory*: it only records.
- A **run** goes through the gates and leaves an **artifact**: logs, reports, checksums, what was tested.

| Status | Meaning | What to do |
|---|---|---|
| `passed` | Everything is fine | Nothing |
| `warn` | Findings below the limit, or an advisory gate failed | Read them when you have time |
| `failed` | **Your code** has a problem | Fix the code |
| `error` | **The machine or a tool** has a problem (missing tool, timeout, unreadable report) | Fix the setup |
| `blocked` | An earlier enforced gate, or a job it needs, failed | Fix that one first |
| `skipped` | Optional tool missing, or nothing to do | Usually nothing |
| `cached` / `reused` | Unchanged since the last pass | Nothing |
| `cancelled` | You stopped it | Run again |

## 1. Install

| System | Command, in the Gatewright folder |
|---|---|
| Linux, macOS | `sh install.sh` (`--dry-run` shows what it would do) |
| Windows | `powershell -ExecutionPolicy Bypass -File install.ps1` (needs Python 3.10+ and Git for Windows) |
| No internet | see [tips.md](tips.md#install-without-internet) |

Then open a new terminal and check the machine:

```bash
gatewright doctor
```

It lists every tool, what is missing, and the command that installs it on *this* system.
To update, get the new source and run the installer again.

## 2. Set up a project

```bash
cd your-project
gatewright init          # Python, Node or Docker starter pipeline, chosen from your files
gatewright validate      # checks the configuration, names every problem at once
gatewright run --dry-run # what would run, in which order, and which tools are missing
```

`init` writes `.gatewright/pipeline.yml` (gates and rules) and `.gatewright/jobs/*.yml` (jobs). Commit both. A job is
short:

```yaml
jobs:
  - id: sast
    gate: commit
    category: sec
    run: bandit -r src -f json -q
    parser: bandit          # how to read the result
```

**Secrets** (tokens, passwords): set `GATEWRIGHT_SECRET_<NAME>` or put `NAME=value` in `.gatewright/secrets.env` (never
commit it), and list the name under `secrets:` of the jobs that need it. Other jobs don't get it.

## 3. Run it

In the **terminal UI**:

```bash
gatewright tui
```

| Key | Does |
|---|---|
| `space` | switch the highlighted gate or category on or off |
| `r` / `d` | run / dry run |
| `f` | re-run only what failed |
| `x` | cancel |
| `m` | gate mode: as configured, all advisory, all enforced |
| `c` | cache on or off |
| `e` `l` `p` `u` | Errors, Log, Report, Queue tab |
| `a` / `n` | Queue tab: approve / reject |
| `o` | open a build on this computer |
| `q` | quit (cancels a running run) |

Or on the **command line**:

```bash
gatewright run                      # all gates
gatewright run -g commit            # one gate
gatewright run -c sec               # only security jobs
gatewright run -j unit-tests        # one job and what it needs
gatewright run --mode advisory      # don't stop at the first failing gate
gatewright rerun-failed             # after a fix: only what failed
```

Exit codes for scripts: `0` passed or warnings, `1` failed, `2` configuration or usage error, `130` cancelled.

Gatewright starts jobs only while CPU and RAM allow, and stops the newest job before the machine runs out of memory. Set
your own limits with `limits:`, see [resources.md](resources.md).

## 4. Read the result

Every problem has a sentence that says what is wrong, where, and how to fix it, with the line of code marked in red.

```bash
gatewright show          # the errors of the last run again
gatewright diff          # what is new or fixed since the run before
gatewright history       # earlier runs
```

The artifact is in `.gatewright/runs/<run>/`: `report.html` and `report.md` to read, `results.sarif` and `junit.xml` for
other tools, logs, and `checksums.sha256`. `gatewright verify` proves that nobody changed it.

**Accepting a finding** is a decision with a reason and an end date, not a hidden switch:

```bash
gatewright baseline create --reason "legacy code, ticket #42" --days 60
```

## 5. Test a build on your computer

No git host needed. In the TUI press `o`, then one of:

- type or paste the **full path**
- pick it in the folder list
- `ctrl+o` (folder) or `ctrl+f` (file) for the desktop's own dialog

Then Enter, and `r` to run. On the command line:

```bash
gatewright run --local /path/to/build.zip
```

Folders, `.zip`, `.whl`, `.tar.*` archives and single files work. **Opening a build runs its code as you**, so open only
what you trust that far. It gets no secrets and can't deploy or sign, unless you choose *As a project of its own*.
Details: [getting-started.md](getting-started.md#test-a-build-on-this-computer).

## 6. Let it follow your git host

1. Tell it which repository (`.gatewright/pipeline.yml`):

   ```yaml
   vcs:
     provider: github        # github | gitea | forgejo | gitlab
     repo: you/your-project
     branches: [main, development]
     mode: manual            # every commit waits for you
   ```
2. Give it a token: `export GATEWRIGHT_SECRET_VCS_TOKEN=...` (which permissions:
   [git-integration.md](git-integration.md#host-specific-setup)). With the GitHub CLI: `GATEWRIGHT_SECRET_VCS_TOKEN=$(gh auth token) gatewright watch` keeps it out of files.
3. Start it, and leave it running: `gatewright watch`
4. A new commit appears in the queue, 🟡 on the host. Look at the change, then approve it:
   `gatewright queue` and `gatewright approve 1`, or `u` and `a` in the TUI.
5. Gatewright fetches exactly that commit, runs the gates, and reports ✅ or ❌ per gate on the commit. Pull requests get a
   summary comment.

Approving means running that code on your machine. For pull requests from strangers set `untrusted: containers` or
`untrusted: refuse`. To check *before* pushing: `gatewright hook install`.

## 7. Release, deploy, roll back

```bash
gatewright keygen --identity you@example.com --add   # once; add --passphrase to use ssh-agent
gatewright verify --require-signature
gatewright promote --to staging                      # the tested artifact, nothing is rebuilt
gatewright promote --to production
gatewright rollback production
gatewright deployments                               # what is live where
```

Needs `signing:` and `environments:` in the pipeline: [releases.md](releases.md). Get told about it:
[notifications.md](notifications.md).

## 8. Keep it healthy

| When | Do |
|---|---|
| Weekly | `gatewright clean --dry-run`, then `gatewright clean --keep 50` (deployed runs are always kept), or `keep_runs: 50` |
| After installing tools | `gatewright doctor` |
| After editing the pipeline | `gatewright validate` |
| Before an important release | `gatewright run --no-cache` |
| Baselines | review `.gatewright/baseline.yml` when entries expire |

Back up `.gatewright/gatewright.db`, deployed runs, and the signing key.

## When something goes wrong

| You see | Try |
|---|---|
| `could not be found on this machine` | `gatewright doctor` and the command it shows |
| `could not read its results` | the tool's report is missing or damaged: read the job's log (`l`) |
| jobs `waiting` | CPU or RAM is above `limits:`; wait, or raise them |
| `blocked` | fix the failing gate before it, or look ahead with `m` (advisory) |
| nothing in the queue | is `gatewright watch` running? Is the branch in `vcs.branches`? |
| a result you don't believe | `gatewright run --no-cache` |

More: [troubleshooting.md](troubleshooting.md), [windows.md](windows.md), [known-issues.md](known-issues.md).
