# Windows

> **Status: beta.** The test suite passes on a real Windows 11 machine (Python 3.13, Git Bash), but only
> that one machine has been tried so far. See [How to test on Windows](#how-to-test-on-windows).

## Contents
- [Installing on Windows](#installing-on-windows)
- [How Gatewright runs commands on Windows](#how-gatewright-runs-commands-on-windows)
- [Writing job commands that work everywhere](#writing-job-commands-that-work-everywhere)
- [What has been tested, and how](#what-has-been-tested-and-how)
- [Known issues](#known-issues)
- [How to test on Windows](#how-to-test-on-windows)

## Installing on Windows

`install.sh` is for Linux and macOS. On Windows use `install.ps1`:

1. Install **Python 3.10 or newer** from python.org.
2. Install **Git for Windows** (https://git-scm.com/download/win). It includes **Git Bash**, which Gatewright
   uses to run job commands, so the same job files work on Linux, macOS and Windows.
3. Run the installer from the Gatewright folder (no administrator rights needed):

   ```powershell
   powershell -ExecutionPolicy Bypass -File install.ps1            # -DryRun shows what it would do
   ```

   It creates `~\.gatewright-venv` and puts `gatewright.exe` in `~\.local\bin` (`-Prefix` and `-BinDir` change
   that, `-Uninstall` removes both). Running it again upgrades.

4. Check the machine: `gatewright doctor`. The `shell` line should say `auto -> bash`.
   If it says `cmd`, Git Bash was not found (see [W2](#w2-without-git-bash-posix-style-commands-fail)).

## How Gatewright runs commands on Windows

| `shell:` (job or pipeline) | What runs the `run:` command | Notes |
|---|---|---|
| `auto` (default) | **Git Bash** if found, else **cmd.exe** | WSL's `C:\Windows\System32\bash.exe` is skipped on purpose: it runs in a different filesystem |
| `bash` | Git Bash | Fails if Git Bash isn't installed |
| `cmd` | cmd.exe | Commands must use cmd syntax |
| `powershell` | `pwsh` (PowerShell 7) if installed, else Windows PowerShell | Runs with `-NoProfile -NonInteractive` |
| `sh` | `/bin/sh` on Linux/macOS | Don't use it on Windows |

Gatewright looks for Git Bash on PATH first, then in `%ProgramFiles%\Git\bin`, `%ProgramFiles(x86)%\Git\bin`
and `%LOCALAPPDATA%\Programs\Git\bin`.

Other Windows-specific behaviour:

- **Gatewright's own variables are replaced before the command runs.** `$GATEWRIGHT_OUT`, `${GATEWRIGHT_OUT}`,
  `%GATEWRIGHT_OUT%` and `$env:GATEWRIGHT_OUT` (also for `GATEWRIGHT_RUN_ID`, `GATEWRIGHT_JOB`, `GATEWRIGHT_GATE`) all work in
  every shell. Paths are inserted with forward slashes (`C:/proj/.gatewright/runs/...`), which every Windows
  shell accepts.
- **Stopping a job** (timeout, cancel, memory watchdog) kills the whole process tree with `psutil`,
  not just the shell. Jobs start in their own process group (`CREATE_NEW_PROCESS_GROUP`) and first get
  Ctrl+Break and 3 seconds to clean up.
- **`shell: powershell` reports every failure as exit code 1** (that is how `powershell -Command` works).
  Add `; exit $LASTEXITCODE` if a job needs the real one.
- **On the laptop profile**, jobs run at *below normal* priority, the Windows equivalent of `nice`.
- **Python tools print UTF-8** (`PYTHONUTF8=1`), even when the console uses a legacy code page.
- **Paths in reports always use `/`.** A finding gets the same fingerprint on Windows and Linux, so
  baselines and run diffs work across machines.
- **All Gatewright files are UTF-8**, regardless of the Windows code page.

## Writing job commands that work everywhere

- Prefer **one command per job**. Chaining differs between shells: `;` works in bash and PowerShell
  but not in cmd, and `&&` works in bash and cmd but only in PowerShell 7+.
- **Quote paths**: `"$GATEWRIGHT_OUT/report.json"`.
- Use Gatewright's variables instead of shell-specific ones. For other environment variables, `$VAR`
  works in Git Bash, `%VAR%` in cmd, `$env:VAR` in PowerShell.
- For anything non-trivial, write a small Python script and call `python scripts/check.py`. It runs
  the same on every OS.
- If a job really is Windows-only, set `shell: powershell` (or `cmd`) on that job.

## What has been tested, and how

The test suite runs on a real Windows 11 machine, including stopping jobs, long paths, file permissions,
`python` in all three shells and `install.ps1` (`tests/test_winsupport.py`). These parts are also tested on Linux
by **simulating Windows inputs** (`tests/test_portability.py`):

| Area | How it was tested |
|---|---|
| Shell selection (Git Bash vs WSL bash vs cmd vs PowerShell) | Simulated `os.name == "nt"` and fake `which` results |
| `%GATEWRIGHT_OUT%` / `$env:GATEWRIGHT_OUT` expansion | Unit test, plus a real run using cmd-style syntax |
| Killing a process tree without process groups | Real process tree, with `os.name` patched to `nt` |
| `C:\...\file.py:9` error locations | Real pytest/traceback text with Windows paths |
| Windows path → relative, `/`-separated path | `ntpath` (Python's Windows path logic) on Linux |
| Same fingerprint for `app\x.py` and `app/x.py` | Unit test |
| Non-UTF-8 code page | Run with a non-UTF-8 locale; the test fails on the old code and passes now |
| cmd built-ins / PowerShell cmdlets count as available | Unit test |

Not tested: Ctrl+C in a real console, PDF output, and an open webhook port.

## Known issues

Severity: **High** = likely to stop you, **Medium** = wrong or confusing result, **Low** = cosmetic or rare.

| # | Issue | Severity |
|---|---|---|
| [W1](#w1-tested-on-one-windows-11-machine-only) | Tested on one Windows 11 machine only | Medium |
| [W2](#w2-without-git-bash-posix-style-commands-fail) | Without Git Bash, POSIX-style commands fail | High |
| [W3](#w3-cmdexe-built-ins-and-forward-slashes) | cmd.exe built-ins and forward slashes | Medium |
| [W4](#w4-without-a-project-venv-python-is-gatewrights) | Without a project venv, python is Gatewright's | Fixed |
| [W5](#w5-pdf-reports-need-gtkpango) | PDF reports need GTK/Pango | Medium |
| [W6](#w6-stopping-a-job) | Stopping a job | Fixed |
| [W7](#w7-ctrlc-handling-is-untested) | Ctrl+C handling is untested | Medium |
| [W8](#w8-long-paths-over-260-characters) | Long paths over 260 characters | Low |
| [W9](#w9-path-case-and-fingerprints) | Path case and fingerprints | Fixed |
| [W10](#w10-readable-secrets-files) | Readable secrets files | Fixed |
| [W11](#w11-symbols-in-the-old-console) | Symbols in the old console | Low |
| [W12](#w12-non-python-tools-may-print-garbled-æøå) | Non-Python tools may print garbled æøå | Low |
| [W13](#w13-docker-desktop-specifics) | Docker Desktop specifics | Medium |
| [W14](#w14-wsl-bash-is-ignored-on-purpose) | WSL bash is ignored on purpose | Low |
| [W15](#w15-antivirus-may-slow-things-down) | Antivirus may slow things down | Low (unverified) |
| [W16](#w16-hardware-detection-limits) | Hardware detection limits | Low |
| [W17](#w17-windows-installer-script) | Windows installer script | Fixed |
| [W18](#w18-parts-of-the-test-suite-skip-without-git-bash) | Parts of the test suite skip without Git Bash | Low |
| [W19](#w19-signing-needs-a-recent-openssh) | Signing needs a recent OpenSSH | Medium |
| [W20](#w20-webhook-ports-and-the-windows-firewall) | Webhook ports and the Windows firewall | Low |
| [W21](#w21-git-hooks-run-through-git-bash) | Git hooks run through Git Bash | Low |
| [W22](#w22-deploy-and-verify-commands-follow-the-job-shell-rules) | Deploy and verify commands follow the job shell rules | Low |

### W1. Tested on one Windows 11 machine only
**Severity: Medium**

Gatewright was first tested on real Windows in November 2026: Windows 11, Python 3.13, Git Bash and (in an
earlier run) Docker Desktop. Other Windows versions, Windows on ARM and Windows Server are untested. Treat
Windows as **beta** and report problems with the bug template, including `gatewright doctor` output.

### W2. Without Git Bash, POSIX-style commands fail
**Severity: High**

The starter templates (`gatewright init`) use POSIX syntax. For example, the coverage job chains two
commands with `;`. Without Git Bash, Gatewright falls back to cmd.exe and those jobs fail.
`gatewright doctor` warns about this.
**Workaround:** install Git for Windows, or set `shell: powershell` and adapt the commands.

### W3. cmd.exe built-ins and forward slashes
**Severity: Medium**

Gatewright inserts `$GATEWRIGHT_OUT` with forward slashes. Some cmd built-ins (`dir`, `copy`, `type`) read an
unquoted `/something` as an option switch.
**Workaround:** quote the path: `type "%GATEWRIGHT_OUT%/report.txt"`.

### W4. Without a project venv, python is Gatewright's
**Fixed in 0.7.0**

Gatewright's whole `Scripts` folder used to come first on a job's PATH, so its `pip`, `ruff` and `pytest`
shadowed yours. Now only `python` and `python3` point at Gatewright's interpreter (through small wrapper
scripts, as on Linux), then comes your PATH, then Gatewright's tools. With a project `.venv`, that is used.
Jobs run through `cmd` or `powershell` get a `.cmd` wrapper: in a *batch file*, write `call python ...`.

### W5. PDF reports need GTK/Pango
**Severity: Medium**

WeasyPrint needs the Pango library, which pip cannot install on Windows. Without it, Gatewright still
writes Markdown, HTML, SARIF and JUnit reports and tells you PDF is unavailable.
**Workaround:** install Pango through MSYS2 as described in WeasyPrint's documentation
(https://doc.courtbouillon.org/weasyprint/stable/first_steps.html#windows), or print `report.html`
to PDF from a browser.

### W6. Stopping a job
**Fixed in 0.7.0**

Timeout, cancel and the memory watchdog now send Ctrl+Break to the job first and wait up to 3 seconds before
killing what is left. Tools that handle Ctrl+Break can clean up; others end at once. Without a console
(a Windows service) the event can't be sent, and the job is killed as before.

### W7. Ctrl+C handling is untested
**Severity: Medium**

The first Ctrl+C asks Gatewright to cancel and the second forces an exit. Jobs run in their own process
group, so they don't receive the console's Ctrl+C directly; Gatewright stops them. This has not been
tried on a real Windows console.

### W8. Long paths over 260 characters
**Severity: Low**

Gatewright's own copying, checksums and packing of artifacts now handle paths over 260 characters, whatever
the Windows setting. The tools your jobs run may still fail on them. `gatewright doctor` shows the setting.
**Workaround:** enable long paths (registry value `LongPathsEnabled=1`, needs an administrator), or keep
projects close to the drive root.

### W9. Path case and fingerprints
**Fixed in 0.7.0**

Paths of files that exist are now reported with their spelling on disk, so `App\x.py` and `app\x.py` are
one finding. A baseline entry made from a differently spelled path has to be accepted once more.

### W10. Readable secrets files
**Fixed in 0.7.0**

Gatewright now warns when `.gatewright/secrets.env` is readable by a broad group (Everyone, Users,
Authenticated Users, Domain Users, ...) and prints the `icacls` command that fixes it. Access given to a
single named account, or to a custom group, is not reported.

### W11. Symbols in the old console
**Severity: Low**

The legacy console (conhost) may show `✓ ✗ ⚠ 🔴` as boxes.
**Workaround:** use Windows Terminal (the default on Windows 11). Set `NO_COLOR=1` for plain output.

### W12. Non-Python tools may print garbled æøå
**Severity: Low**

Python tools are forced to UTF-8. Other tools that print in the console's OEM code page (cp850 on
Norwegian systems, for example) show non-ASCII characters garbled in the logs. Nothing crashes.

### W13. Docker Desktop specifics
**Severity: Medium**

- **Line endings:** shell scripts checked out with Windows line endings (CRLF) fail inside Linux
  containers with errors like `$'\r': command not found`. Add `*.sh text eol=lf` to `.gitattributes`.
- **Speed:** mounting a folder from `C:\` into a container is much slower than on Linux. Big test
  suites in containers will be slow.
- Container jobs are otherwise unchanged: `docker run --rm`, CPU/memory limits, `--network none`, and
  `docker kill` on timeout/cancel. The `--user` mapping is skipped, since it only applies to Linux.

### W14. WSL bash is ignored on purpose
**Severity: Low**

`C:\Windows\System32\bash.exe` (WSL) runs commands in the Linux filesystem, where Windows paths
don't exist. Gatewright never uses it for `shell: auto`. To use Linux tooling, run Gatewright **inside** WSL instead.

### W15. Antivirus may slow things down
**Severity: Low (unverified)**

Real-time scanning (Microsoft Defender) can slow down creating virtual environments and running
many small tools. It may also briefly lock files while Gatewright writes or packs artifacts. If a run
fails with "permission denied" on a file under `.gatewright\runs`, re-run, or exclude the project folder
from scanning.

### W16. Hardware detection limits
**Severity: Low**

Only NVIDIA GPUs are detected (through `nvidia-smi`). Battery detection uses `psutil` and decides the
laptop/server profile. Load average is estimated from CPU usage, since Windows has none.

### W17. Windows installer script
**Fixed in 0.7.0**

`install.ps1` installs, upgrades and uninstalls Gatewright for your account, without administrator rights.

### W18. Parts of the test suite skip without Git Bash
**Severity: Low**

Tests that write job commands in POSIX syntax are skipped when Git Bash is missing, and Docker tests
need a Linux image with `python` and `sh` (`GATEWRIGHT_TEST_IMAGE`). Skipped tests always state the reason.

### W19. Signing needs a recent OpenSSH
**Severity: Medium**

Windows ships OpenSSH as an optional feature. Older Windows 10 builds have a version that is too old for
`ssh-keygen -Y` (Gatewright needs **8.2+**). Check with `ssh -V`. If it's older, install a current
Win32-OpenSSH release. `gatewright doctor` reports the version when `signing:` is configured.

### W20. Webhook ports and the Windows firewall
**Severity: Low**

`gatewright watch` has run on Windows 11 against a real private GitHub repository: polling, live webhooks through GitHub's
relay and through a Cloudflare tunnel, checkout, approval, statuses and pull request comment. Opening the port itself to
the network has not been tried: Windows Defender Firewall asks before allowing it. Keep `host: 127.0.0.1` behind a reverse
proxy or tunnel.

### W21. Git hooks run through Git Bash
**Severity: Low**

The pre-push hook is a POSIX `sh` script. Git for Windows runs hooks with its bundled `sh`, so it works
when Git for Windows is installed (other git clients may not run it). `gatewright` must be on the PATH
that Git Bash sees.

### W22. Deploy and verify commands follow the job shell rules
**Severity: Low**

`deploy:`/`verify:` run like jobs: Git Bash if available, else cmd.exe. Use `$GATEWRIGHT_ARTIFACT`
(works in every shell, because Gatewright substitutes it), or write them as Python scripts.

## How to test on Windows

```powershell
git clone <your fork> gatewright; cd gatewright
py -m venv .venv; .venv\Scripts\activate
pip install -e ".[dev,tools]"
pytest -v                 # one test, test_real_windows_smoke, runs only on Windows
gatewright doctor
cd $env:TEMP; mkdir gatewright-demo; cd gatewright-demo; gatewright init --template python; gatewright run --dry-run
gatewright tui                 # try r, d, x, q in Windows Terminal
```

When you report results, include: Windows version, terminal (Windows Terminal / conhost / VS Code),
`python --version`, whether Git Bash is installed, the `pytest` summary line, and `gatewright doctor` output.

**Testing on GitHub:** `.github/workflows/tests.yml` runs the suite on a clean Windows machine for every pull request.
