# Troubleshooting

Start with `gatewright doctor`. It checks the OS, Python, the shell, Docker, PDF support and every tool
your jobs use, and prints the install command for **your** system.

## Installation

| Symptom | Cause and fix |
|---|---|
| `gatewright: command not found` after installing | `~/.local/bin` is not on PATH. Add `export PATH="$HOME/.local/bin:$PATH"` to `~/.bashrc` / `~/.zshrc`, then open a new terminal. |
| `error: externally-managed-environment` | You ran `pip install` against the system Python (Kali, Debian, Ubuntu, Fedora, Arch block that). Use `install.sh`, `pipx`, or a venv. |
| `... has no venv module. Install it: sudo apt install python3-venv` | Debian-based systems ship `venv` separately. Install it, or re-run `install.sh` without `--no-system`. |
| `Need root to run: ... (no sudo/doas found)` | Run the printed command as root, then `sh install.sh --no-system`. |
| `Could not install: ... Check your network/mirror` | Your package mirror is unreachable. Fix the network/mirror, or install the listed packages yourself and use `--no-system`. |
| `No Python >= 3.10 with venv found` | Install a newer Python (e.g. `python3.12` + `python3.12-venv`) or pass `--python /path/to/python3.12`. |
| PDF reports unavailable | Pango is missing. `gatewright doctor` shows the package for your OS (e.g. `sudo apt install -y libpango-1.0-0 libpangoft2-1.0-0`). Markdown/HTML reports always work. |

## Running

| Symptom | Cause and fix |
|---|---|
| `⚠ INFRA ... The command 'X' could not be found` | The tool isn't installed or not on PATH. `gatewright doctor` prints how to install it. Mark the job `optional: true` if it's nice-to-have. |
| `Python could not import the module 'pytest'` | The Python your jobs use lacks it. `gatewright doctor` shows **which** Python (project `.venv`, Gatewright's own, or PATH). Install it there, e.g. `.venv/bin/pip install pytest`. |
| Tests can't import your project's dependencies | Your project has no venv, so jobs use Gatewright's Python. Create one: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest`. Gatewright picks it up automatically. |
| A job never starts: `waiting: memory budget (...)` | Its declared `mem` plus running jobs exceed the budget. It starts once others finish. Lower `resources.mem`, or raise limits (see [resources.md](resources.md)). |
| `Gatewright stopped this job to protect the machine` | System RAM passed `kill_mem_percent`. Run fewer jobs in parallel (`limits.max_parallel`), or raise the job's `resources.mem` so it isn't started next to others. |
| `did not finish within its time limit` | Raise `timeout:`, or check the log for where it hangs (waiting for input or network are common). |
| `exited with code N and the tool produced no output` / `was not valid JSON` | The tool crashed or got wrong arguments, so Gatewright could not read results (it never treats that as "no findings"). The raw output is in the error panel and `logs/<job>.log`. |
| A gate is `blocked` with `requires gate(s) ... to have passed for this commit` | Run the required gate first for this commit (`gatewright run -g test`), or include it in the run. Needs git. |
| Old findings block every run | Accept them with a reason and expiry: `gatewright baseline create --reason "..." --days 60`, then review `.gatewright/baseline.yml`. |
| `suppression ... expired` warning | A baseline entry's `expires` date passed. Fix the finding or extend the date (with a reason). |
| Job unexpectedly `cached` | Its inputs, definition and tool version didn't change since a passing run. Use `--no-cache` or `gatewright cache-clear`. |

## Containers (`image:`)

| Symptom | Cause and fix |
|---|---|
| `Docker is not running or not reachable` | Start Docker (`sudo systemctl start docker`, or open Docker Desktop). On Linux, your user must be in the `docker` group (log out and in after adding). |
| `The container image ... is not available` | `docker pull <image>` or build it first. Check the name and your registry login. |
| Out of memory, exit 137 | The job hit its `resources.mem` container limit. Raise it. |
| Permission errors inside the container | The container runs as your user on Linux. For images that need root, set `container_user: image` on the job. |
| `$'\r': command not found` | A script has Windows line endings. Add `*.sh text eol=lf` to `.gitattributes`. |

## Git integration (watch, queue, hook)

| Symptom | Cause and fix |
|---|---|
| `API ... failed with HTTP 401 ... token is missing or invalid` | Set the token (`GATEWRIGHT_SECRET_VCS_TOKEN`, or the name in `vcs.token_secret`); check it hasn't expired. |
| `HTTP 403 ... permission ... or rate limit` | Give the token the permissions in [git-integration.md](git-integration.md#host-specific-setup), or raise `poll_interval`. |
| `HTTP 404 ... repository was not found` | Check `vcs.repo` (GitLab: full path) and that the token can see the repository. |
| `Could not reach the ... API` | Wrong `api_url`, no network, or a proxy. Gitea/Forgejo need `api_url` ending in `/api/v1`. |
| Commits never run | Nothing approved them: `gatewright queue`, then `gatewright approve <id>`. Is a watcher running? The TUI Queue tab shows it. |
| Item in state `error`: `git fetch failed` | The commit is gone (force-push) or the token can't read the repository. |
| `refusing to run` (checked-out SHA differs) | The host returned a different commit than approved. Gatewright refused on purpose; approve the new commit. |
| A fork PR job fails with `Secrets are withheld` | By design for untrusted runs. Review the change; run it from a trusted branch if it needs secrets. |
| Webhook deliveries get 401 | The secret on the host differs from `vcs.webhook.secret`'s value, or the host signs differently (see the table in git-integration.md). |
| `git push` is refused by the hook | The commit gate failed. Fix it, or skip once with `git push --no-verify`. |

## Signing and deployments

| Symptom | Cause and fix |
|---|---|
| `artifact NOT signed: ... ssh-keygen ... not installed` | Install OpenSSH client 8.2+ (`gatewright doctor` shows the command for your OS). |
| `Signing key ... not found` | `gatewright keygen --identity you@example.com --add`, then set `signing.key`. |
| `the signing key is not in allowed_signers ... not trusted` | Add the signer's line (printed by `keygen`) to `.gatewright/allowed_signers`. |
| `signature does NOT match` | The artifact or its checksums changed after signing. Don't deploy it; re-run the pipeline. |
| `Run ... cannot go to production:` + reasons | Every listed reason must be fixed (e.g. deploy to staging first, or re-run the failed gate). |
| `Not confirmed ... requires approval` | Answer `y`, or pass `--yes` in scripts. |
| Deployment `rolled_back` | `verify` failed after deploying; the note says why, and the logs are in the deployment folder. |
| `Nothing to roll back to` | The environment has had only one successful artifact. |
| `notification via ... failed` warning | Server/URL/token problem. Run `gatewright notify-test`. The run itself is unaffected. |
| `unknown key (True was unexpected)` | You're on an old version that read `on:` as a boolean. Update Gatewright (0.6+ reads YAML 1.2 booleans). |

## Configuration

`gatewright validate` lists every problem with file and location. Common ones:

| Message | Fix |
|---|---|
| `unknown key ('colour' was unexpected)` | Typo in a key. See [configuration.md](configuration.md). |
| `must be one of ['dev', 'sec', 'ops'], got 'qa'` | Use one of the listed values. |
| `uses unknown gate 'deploy'` | Add the gate to `pipeline.yml`, or fix the name. |
| `cannot need 'x' from a later gate` | A job can only depend on jobs in the same or an **earlier** gate. |
| `dependency cycle detected: a -> b -> a` | Remove one of the `needs`. |
| `invalid YAML` with line and column | Usually indentation or a missing quote. Quote values with `:` or `#` in them. |

## Still stuck?

Open an issue with the output of `gatewright --version`, `gatewright doctor`, the failing command with `-v`,
and the relevant `logs/<job>.log`. **Remove secrets first**, even though Gatewright masks the ones it knows about.
