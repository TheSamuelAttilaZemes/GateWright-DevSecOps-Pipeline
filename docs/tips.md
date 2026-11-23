# Tips and recipes

Practical setups that make Gatewright nicer to live with.

- [A fully self-hosted setup (no Microsoft, no cloud)](#a-fully-self-hosted-setup-no-microsoft-no-cloud)
- [Run the watcher as a service](#run-the-watcher-as-a-service)
- [Receiving webhooks on a home machine](#receiving-webhooks-on-a-home-machine)
- [Make runs faster](#make-runs-faster)
- [Keep the disk tidy](#keep-the-disk-tidy)
- [Kali Linux](#kali-linux)
- [Writing deploy and verify scripts](#writing-deploy-and-verify-scripts)
- [Handling old findings](#handling-old-findings)
- [YAML gotchas](#yaml-gotchas)

## A fully self-hosted setup (no Microsoft, no cloud)

1. Run **Forgejo** (or Gitea) on a small server or a home machine. It's a single binary or container.
2. Install Gatewright on the same machine (or another one): `sh install.sh`.
3. In your project: `vcs: {provider: forgejo, repo: you/app, api_url: https://git.example.lan/api/v1}`,
   with a Forgejo access token as `VCS_TOKEN`.
4. Run `gatewright watch` as a service (below), with a webhook pointing at it, or just polling.
5. Make `gatewright/commit` and `gatewright/test` required status checks on your main branch
   (in Forgejo/Gitea: the branch protection rule's status-check settings).
6. Add `signing:`, `environments:` and an ntfy or Matrix channel for alerts.

That's the whole loop on your own hardware: push, approval, pipeline, ✅/❌, signed artifact,
promote, rollback.

## Run the watcher as a service

**Linux (systemd, per user):** create `~/.config/systemd/user/gatewright-watch.service`:

```ini
[Unit]
Description=Gatewright watcher for my-project
After=network-online.target

[Service]
WorkingDirectory=%h/src/my-project
ExecStart=%h/.local/bin/gatewright watch
Restart=on-failure
EnvironmentFile=%h/.config/gatewright/my-project.env

[Install]
WantedBy=default.target
```

Put `GATEWRIGHT_SECRET_VCS_TOKEN=...` in the env file (`chmod 600` it). Then run:

```bash
systemctl --user daemon-reload && systemctl --user enable --now gatewright-watch
loginctl enable-linger $USER        # keep it running when you're logged out
journalctl --user -u gatewright-watch -f
```

**Cron instead of a service:** `*/5 * * * * cd ~/src/my-project && ~/.local/bin/gatewright watch --once`
polls every 5 minutes and runs whatever was approved.

## Receiving webhooks on a home machine

Keep the webhook listening on `127.0.0.1` (the default) and put something in front of it:

- **Same network as the git server:** point the webhook at the machine's LAN address through a
  reverse proxy (Caddy, nginx) with TLS.
- **From the internet without opening ports:** a tunnel such as Cloudflare Tunnel, Tailscale Funnel or
  an SSH reverse tunnel to a small VPS.
- **Always set a webhook secret.** Unsigned or wrongly signed deliveries get HTTP 401.
- If you can't receive webhooks, polling every 60 seconds is fine for most projects.

## Make runs faster

- Give every job narrow `inputs:` globs. Unchanged inputs mean a cached result.
- Use `when_exists:` for jobs that only make sense when a file exists (e.g. `[requirements.txt]`,
  `[Dockerfile]`). Otherwise they're skipped with a note instead of failing.
- Raise `limits.max_parallel` on a server. Declare realistic `resources.mem` so the scheduler can pack jobs.
- Put the expensive jobs (full test suite, container scans) in a later gate, so a failing lint stops the run early.

## Keep the disk tidy

```bash
gatewright clean --dry-run            # what would be deleted (all but the newest 20 runs)
gatewright clean --keep 50 --days 30  # keep the newest 50, and everything younger than 30 days
```

Or let every run do it: `keep_runs: 50` in `pipeline.yml`. Runs that were ever deployed are always kept
(rollbacks need their artifacts), and so is the history. Folders of crashed runs aren't in the history and stay.

## Install without internet

On a connected machine with the same OS and Python version, from the Gatewright folder:

```bash
python3 -m pip download -d wheels ".[tools]" "setuptools>=77" wheel
```

Copy the folder, then: `sh install.sh --offline wheels --no-pdf`, or on Windows
`powershell -ExecutionPolicy Bypass -File install.ps1 -Offline wheels`. Python and git must already be there.

## Kali Linux

- `sh install.sh` uses apt and a separate venv. Kali's system Python stays untouched (Kali blocks
  system-wide `pip install`).
- Kali packages **gitleaks** and **trivy**: `sudo apt install -y gitleaks trivy`, and `gatewright doctor`
  suggests exactly that. The starter template's secret scan then runs instead of being skipped.
- `openssh-client` is installed by default, so signing works out of the box.
- Running as root is common on Kali. Gatewright works as root, but container jobs then also run as
  root inside the container. A normal user with sudo is the safer default.

## Writing deploy and verify scripts

- Make `deploy` **idempotent** (running it twice is harmless) and make it fail loudly (non-zero exit)
  on any error.
- Read what to deploy from `$GATEWRIGHT_ARTIFACT` (e.g. `$GATEWRIGHT_ARTIFACT/out/build/app.whl`).
  Never rebuild.
- Make `verify` check the **live** system: a health endpoint, a version endpoint returning
  `$GATEWRIGHT_COMMIT`, a smoke login. If `verify` doesn't really check the new version, automatic
  rollback can't protect you.
- Keep credentials in `secrets:` (they're masked in logs), not in the script.
- Python scripts (`python3 scripts/deploy.py`) run the same on Linux, macOS and Windows.

## Handling old findings

- Accept a backlog once with `gatewright baseline create --days 60 --reason "backlog, ticket #12"`, then
  review `.gatewright/baseline.yml` and give important entries their own reason.
- Use short expiries. An expired suppression makes the finding active again and the run warns.
- `gatewright diff` after each run shows what's **new**. Focus on that.

## YAML gotchas

- Gatewright reads YAML with **YAML 1.2 booleans**: only `true`/`false` are booleans. `yes`, `no`,
  `on`, `off` are plain text, so `on: [run_failed]` works and `cache: yes` is rejected with a clear error.
- Quote values that contain `: ` or start with special characters: `run: 'python -c "print(1)"'`.
- Run `gatewright validate` after every edit. It lists every problem with file and location at once.
