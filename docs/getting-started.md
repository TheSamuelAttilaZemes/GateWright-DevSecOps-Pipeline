# Getting started

## 1. Install

### Linux (any distribution) and macOS

```bash
git clone <repository-url> gatewright && cd gatewright
sh install.sh --dry-run   # optional: see exactly what it will do on this machine
sh install.sh             # asks before installing system packages; --yes to skip the question
```

What the installer does:

1. **Detects the OS and distribution** from `/etc/os-release`. If it doesn't know the distribution,
   it checks `ID_LIKE`, then which package manager exists. It also notices WSL.
2. **Installs system packages with that package manager.** It needs Python ≥ 3.10 with `venv`, git,
   and the Pango libraries for PDF reports:

   | Family | Distributions (examples) | Package manager |
   |---|---|---|
   | debian | Kali, Debian, Ubuntu, Linux Mint, Parrot, Pop!_OS, Raspberry Pi OS, Devuan | apt |
   | rhel | Fedora, RHEL, Rocky, AlmaLinux, CentOS Stream, Oracle Linux, Amazon Linux | dnf / yum |
   | arch | Arch, Manjaro, EndeavourOS, Garuda, BlackArch, Artix | pacman |
   | suse | openSUSE Tumbleweed / Leap, SLES | zypper |
   | alpine | Alpine, postmarketOS | apk |
   | void | Void | xbps |
   | gentoo | Gentoo, Funtoo | emerge |
   | nixos | NixOS | prints a `nix-shell -p ...` line (NixOS is declarative) |
   | macos | macOS | Homebrew |

   If the system Python is older than 3.10 (e.g. RHEL 8), it tries `python3.12`/`python3.11` packages.
   If the PDF libraries can't be installed, everything else still works.
3. **Installs Gatewright into its own virtual environment** at `~/.local/share/gatewright/venv`. It never touches
   the system Python, which most current distributions protect (PEP 668: Kali, Debian 12+, Ubuntu 23.04+,
   Fedora, Arch). The starter scanners (ruff, bandit, pytest, coverage, pip-audit) go into the same environment.
4. **Links `gatewright`** into `~/.local/bin` and checks that the CLI, the terminal UI and PDF support load.
   If `~/.local/bin` is not on your PATH, it prints the line to add to `~/.bashrc` or `~/.zshrc`.

Options:

| Option | Effect |
|---|---|
| `-y`, `--yes` | Don't ask before installing system packages |
| `-n`, `--dry-run` | Print every command, change nothing |
| `--no-system` | Skip system packages (Python ≥ 3.10 and git already installed) |
| `--no-pdf` | Skip the PDF libraries |
| `--no-tools` | Don't install ruff, bandit, pytest, coverage, pip-audit |
| `--with-docker` | Also install Docker (Podman on RHEL-likes other than Fedora) |
| `--prefix DIR`, `--bin-dir DIR` | Install locations (or `GATEWRIGHT_PREFIX`, `GATEWRIGHT_BIN_DIR`) |
| `--python PATH` | Use a specific interpreter |
| `--uninstall` | Remove Gatewright (your projects' `.gatewright/` folders stay) |

The script is POSIX `sh` and runs under bash, dash and BusyBox ash. It uses `sudo` or `doas` for
system packages. Without either, it tells you what to run as root.

### Windows

See [windows.md](windows.md#installing-on-windows). In short: Python 3.10+, Git for Windows, then
`pipx install ".[tools]"`.

### For development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,tools,pdf]"
pytest
```

## 2. Create a pipeline

```bash
cd your-project
gatewright init                 # picks python / node / docker from your files, or: --template python
gatewright doctor               # what's installed, what's missing, and how to install it on THIS OS
gatewright run --dry-run        # gates, jobs, parallel waves, resource needs, missing tools (same as: gatewright plan)
```

`gatewright init` writes `.gatewright/pipeline.yml`, `.gatewright/jobs/*.yml` and `.gitignore` entries. The Python
template has these gates:

| Gate | Mode | Jobs |
|---|---|---|
| commit | enforced | lint (ruff), format, secrets-scan (gitleaks, optional), sast (bandit), deps-audit (pip-audit, optional) |
| build | enforced | compile |
| test | enforced | unit-tests (pytest → JUnit), coverage (optional) |
| staging | advisory, **disabled** | smoke (placeholder for your deploy + health check); requires `test` |

## 3. Run it

```bash
gatewright run                          # all enabled gates
gatewright run -g commit                # one gate
gatewright run -c sec                   # only security jobs (+ what they depend on)
gatewright run -j unit-tests            # one job (+ dependencies)
gatewright run --mode advisory          # run every gate even if an earlier one fails
gatewright run --coverage test=90       # require 90% coverage in the test gate, this run only
gatewright run -v                       # stream every job's output
gatewright run --report md,pdf          # choose report formats
gatewright tui                          # the same, in the terminal UI
```

The result shows a summary table, then one panel per error with a plain-language explanation, a fix,
and the code with the failing line in red. The exit code is 0 (passed, warnings, or nothing to run), 1 (failed),
2 (configuration error) or 130 (cancelled with Ctrl+C), so you can use `gatewright run` in scripts and git hooks.

## 4. Work with the results

```bash
gatewright show                         # errors of the last run again
gatewright show --job sast --all        # including suppressed findings
gatewright history                      # previous runs
gatewright diff                         # new / fixed findings vs the previous run
gatewright rerun-failed                 # fix something, re-run only what failed
gatewright report -f pdf                # regenerate a report format
gatewright verify                       # check the artifact's checksums
gatewright baseline create --reason "legacy code, ticket #42" --days 60
```

Each run's artifact is in `.gatewright/runs/<run-id>/`, plus `gatewright-run-<run-id>.tar.gz`.
See [errors-and-reports.md](errors-and-reports.md#what-to-do-with-artifacts) for what to do with it.

## 5. The terminal UI

```bash
gatewright tui
```

| Key | Action |
|---|---|
| `space` | Toggle the highlighted gate/category |
| `o` | Open a build on this computer: a folder, archive or file (below) |
| `r` | Run the selected gates and categories |
| `d` | Dry run (plan in the Log tab) |
| `f` | Re-run what failed in the last run |
| `x` | Cancel the running pipeline |
| `c` | Cache on/off |
| `m` | Gate mode: as configured → all advisory → all enforced |
| `e` / `l` / `p` / `u` | Errors / Log / Report / Queue tab |
| `a` / `n` | In the Queue tab: approve / reject the highlighted commit |
| `↑` `↓` | Select a job: its errors appear in the Errors tab |
| `q` | Quit |

In the History tab, press Enter on a run to load it.

### Test a build on this computer

No git host is needed. Press `o`, then type or paste a **full path**, pick one in the folder list, or open the
desktop's dialog with `ctrl+o` (folder) or `ctrl+f` (file): Explorer on Windows and WSL, zenity/kdialog/yad on Linux,
Finder on macOS. Over SSH, without a desktop, use the path or the list. Outside a project, `gatewright tui` asks
at once. Without the TUI:

```bash
gatewright run --local ~/builds/app-1.2.tar.gz          # .zip .whl .tar .tar.gz .tar.bz2 .tar.xz, a folder or a file
gatewright run --local . --pipeline template            # auto (default) | own | project | template
gatewright tui --local /path/to/folder
```

| Pipeline | What runs |
|---|---|
| `auto` | The jobs of its own pipeline if it has one, else a template that fits its files |
| `own` | Its own pipeline. A **folder** then opens as a normal project, with its secrets and settings |
| `project` | The jobs of the project Gatewright was started in |
| `template` | The Python, Node or Docker starter pipeline |

Opening a subject **runs its code** (tests, build scripts) as you: only open what you trust that far. Except for
a folder opened with `own`, it runs without secrets and can't deploy, sign or notify. Its results are kept apart
from every project, in `%LOCALAPPDATA%\gatewright\local` or `~/.local/state/gatewright/local`; `run --local` prints
the folder to use with `gatewright -C`. Folders are tested in place, archives in an unpacked copy.

## 6. Connect your git host, sign, deploy, get notified

These are optional. Add them when you need them.

```bash
# git integration (v0.5): queue commits, approve, report ✅/❌ per gate
gatewright watch                     # with a `vcs:` section in pipeline.yml
gatewright queue && gatewright approve 3
gatewright hook install              # run the commit gate before every git push

# trust and release (v0.6)
gatewright keygen --identity you@example.com --add     # then add `signing:` to pipeline.yml
gatewright verify --require-signature
gatewright promote --to staging      # with `environments:` in pipeline.yml
gatewright promote --to production --yes
gatewright rollback production
gatewright deployments
gatewright release-notes --run last
gatewright notify-test               # with `notify:` in pipeline.yml
```

Guides: [git-integration.md](git-integration.md), [releases.md](releases.md),
[notifications.md](notifications.md), and practical setups in [tips.md](tips.md).

## Next steps

- Adapt the jobs: [configuration.md](configuration.md)
- Add a tool or report format: [extending.md](extending.md)
- Control how much of the machine Gatewright uses: [resources.md](resources.md)
