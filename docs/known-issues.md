# Known issues and limitations

An honest map of what is untested, limited or rough in **Gatewright Pipeline 0.7** (0.7.2).
Windows has its own detailed list: [windows.md](windows.md#known-issues) (W1–W22).
Found something else? Open an issue with the bug or platform-report template.

Severity: **High** = likely to stop you or mislead you, **Medium** = wrong or confusing in some
setups, **Low** = cosmetic, rare, or a missing convenience.

## Map

| ID | Area | Issue | Severity |
|---|---|---|---|
| [T1](#t1-macos-has-only-run-in-tests) | Platforms | macOS has only run in tests | Medium |
| [T2](#t2-github-is-only-partly-tested-for-real) | Git integration | GitHub is only partly tested for real | Low |
| [T3](#t3-slack-discord-and-mattermost-are-untested) | Notifications | Slack, Discord and Mattermost are untested | Low |
| [T4](#t4-gentoo-and-nixos-are-only-dry-run-tested) | Platforms | Gentoo and NixOS are only dry-run tested | Low |
| [T5](#t5-npm-audit-and-trivy-on-other-project-types) | Parsers | npm audit and trivy on other project types | Low |
| [T6](#t6-openssh-82-to-88-are-untested) | Signing | OpenSSH 8.2 to 8.8 are untested | Low |
| [T7](#t7-rootless-docker-is-untested) | Containers | Rootless Docker is untested | Low |
| [T8](#t8-pip-audit-on-debian) | Parsers | pip-audit on Debian | Fixed |
| [L1](#l1-untrusted-code-still-runs-when-you-approve-it) | Security | Untrusted code still runs when you approve it | Medium |
| [L2](#l2-secret-masking-knows-common-encodings-only) | Security | Secret masking knows common encodings only | Low |
| [L3](#l3-the-signing-key-has-no-passphrase-by-default) | Signing | The signing key has no passphrase by default | Low |
| [L4](#l4-no-locking-between-deployments) | Deployments | No locking between deployments | Fixed |
| [L5](#l5-only-the-first-page-of-branches-and-pull-requests) | Git integration | Only the first page of branches and pull requests | Fixed |
| [L6](#l6-closed-pull-requests-stay-in-the-queue) | Git integration | Closed pull requests stay in the queue | Fixed |
| [L7](#l7-the-watcher-runs-one-commit-at-a-time) | Git integration | The watcher runs one commit at a time | Fixed |
| [L8](#l8-shallow-checkouts-no-submodules-or-lfs) | Git integration | Shallow checkouts, no submodules or LFS | Fixed |
| [L9](#l9-old-runs-are-never-deleted) | Disk | Old runs are never deleted | Fixed |
| [L10](#l10-yes-no-on-off-are-no-longer-booleans) | Config | `yes`/`no`/`on`/`off` are no longer booleans (YAML 1.2) | Low |
| [L11](#l11-duplicate-findings-can-merge) | Findings | Duplicate findings can merge | Fixed |
| [L12](#l12-caching-hashes-every-file-by-default) | Speed | Caching hashes every project file unless `inputs:` is set | Low |
| [L13](#l13-results-depend-on-your-tools-global-config) | Findings | Tools may use your personal global config (e.g. ruff) | Low |
| [L14](#l14-containers-run-as-your-user) | Containers | Containers run as your user | Fixed |
| [L15](#l15-notifications-are-plain-text) | Notifications | Notifications are plain text | Low |
| [L16](#l16-no-update-command) | Install | No update command | Low |
| [L17](#l17-the-tui-cant-edit-baselines) | UX | The TUI can't edit baselines | Low |
| [L18](#l18-100-coverage-is-enforced-on-linux-only) | Testing | Gatewright's 100% coverage rule is enforced on Linux only | Low |

## Not verified on real systems

### T1. macOS has only run in tests
**Severity: Medium** · The test suite passed on a GitHub-hosted macOS runner (0.7.1). macOS is not part of CI, so newer changes
may break it unnoticed. The installer, the TUI and the file dialog have
not been used on a Mac. Windows is tested on one Windows 11 machine and in CI: [windows.md](windows.md).

### T2. GitHub is only partly tested for real
**Severity: Low** · Tested in November 2026 with **real Gitea 1.24, Forgejo 11 and GitLab CE 18.5** servers, with the token
scopes from [git-integration.md](git-integration.md): private clones, statuses, approvals, (fork) pull requests, comments,
more than 100 branches, closed pull requests and signed webhooks. On real **GitHub** (private repository, from Windows):
clone, approval, statuses for passing and failing commits, a pull request with its comment, a pull request from a fork in
another account (queued as untrusted), live webhooks through GitHub's relay and on a public address, forged ones refused,
and polling `microsoft/vscode` (read only). A fine-grained token with exactly the documented permissions lists, fetches (also the fork's pull request), posts
statuses and comments, and sees no other repository. The fork's job definitions and baseline were ignored. Untested on
GitHub: running into the rate limit for real (the pause is tested with a fake answer). With thousands of open pull requests the first poll takes minutes; use
webhooks and `pull_requests: false` there. Right after the very first push to a new Gitea/Forgejo repository the API
answers 404 for some seconds; the next poll works. **Workaround:** start with `mode: manual` and `gatewright watch --once`.

### T3. Slack, Discord and Mattermost are untested
**Severity: Low** · Tested with real servers: ntfy, Matrix (Synapse), and SMTP with STARTTLS, TLS and login. The webhook's
formatting in Slack, Discord and Mattermost is untested. **Workaround:** `gatewright notify-test`.

### T4. Gentoo and NixOS are only dry-run tested
**Severity: Low** · Real installs plus run, sign, verify, promote, release notes, push hook, clean and PDF pass (as root, in containers) on
Debian 12, Ubuntu 22.04, Kali, Fedora 42, Rocky 9, Arch, openSUSE Tumbleweed, Void and Alpine 3.22. The test suite
runs on Ubuntu 24.04 (WSL 2). Gentoo and NixOS: installer dry runs only.

### T5. npm audit and trivy on other project types
**Severity: Low** · gitleaks 8.24, trivy 0.62, hadolint 2.12 and semgrep have run for real, and the Node and Docker templates end to end.
Other versions and other scan types may print something new.

### T6. OpenSSH 8.2 to 8.8 are untested
**Severity: Low** · Signing works with OpenSSH 8.9 to 10.6. Gatewright needs 8.2+; 8.7 has a known crash in `find-principals`, which
`gatewright doctor` warns about.

### T7. Rootless Docker is untested
**Severity: Low** · Docker Desktop (Windows and WSL), registry pulls and rootless Podman (`docker` linked to podman) work. Rootless Docker
is detected and tested with a fake only.

### T8. pip-audit on Debian
**Fixed in 0.7.0** · Could not be reproduced: pip-audit works on Debian 12 with Debian's own Python.

## Limitations

### L1. Untrusted code still runs when you approve it
**Severity: Medium** · For fork pull requests Gatewright withholds secrets and uses **your** job definitions, but approving still runs
their code. Set `vcs.untrusted: containers` to enforce containers without network, or `refuse`. The default is
still `run`. Containers are no perfect wall: they share the kernel and can write to the checkout.

### L2. Secret masking knows common encodings only
**Severity: Low** · Masked: the value, base64 (also inside longer text), hex, URL and JSON encoding, and each line of a multi-line
secret. Not masked: compressed or encrypted output, a value split over lines, encodings of secrets shorter than 8
characters, binary files and text files over 32 MiB. Secrets shorter than 4 characters are never masked.

### L3. The signing key has no passphrase by default
**Severity: Low** · So that unattended runs can sign. `gatewright keygen --passphrase` protects the key; signing then uses ssh-agent.
Security keys go through the agent too, but are untested.

### L4. No locking between deployments
**Fixed in 0.7.0** · One deployment per environment at a time, also between processes. Projects on a network drive depend on its locks.

### L5. Only the first page of branches and pull requests
**Fixed in 0.7.0** · Every page is read, up to 100 pages.

### L6. Closed pull requests stay in the queue
**Fixed in 0.7.0** · The next poll removes pending items of closed or merged pull requests.

### L7. The watcher runs one commit at a time
**Fixed in 0.7.0** · `vcs.parallel: 2` (up to 16). The default is still 1.

### L8. Shallow checkouts, no submodules or LFS
**Fixed in 0.7.0** · `vcs.fetch_depth: 0`, `vcs.submodules: true` and `vcs.lfs: true`. Submodules and LFS are for trusted commits only: an
untrusted one must not choose which servers are contacted. LFS is tested without a real LFS server.

### L9. Old runs are never deleted
**Fixed in 0.7.0** · `gatewright clean` or `keep_runs:`. Folders of crashed runs are not in the history and stay.

### L10. `yes`, `no`, `on`, `off` are no longer booleans
**Severity: Low** · By design (YAML 1.2). Since 0.6, config files are read with YAML 1.2 booleans, so `notify: on:` works.
A config that used `cache: no` must now say `cache: false`. `gatewright validate` points it out.

### L11. Duplicate findings can merge
**Fixed in 0.7.0** · Identical lines are separate findings. A baseline entry covers the first of them, so others it used to hide show up.

### L12. Caching hashes every file by default
**Severity: Low** · By design: skipping `.gitignore`d files would reuse results when an ignored input (a `.env`, generated code) changes. Without `inputs:`, a job's cache key covers every project file (minus a fixed ignore
list; `.gitignore` isn't used). Set narrow `inputs:` on big repositories.

### L13. Results depend on your tools' global config
**Severity: Low** · By design: Gatewright doesn't hide your configuration from your tools. ruff and other tools read personal config files (e.g. `~/.config/ruff`), so findings can
differ between machines. Put the tool's config in the project.

### L14. Containers run as your user
**Fixed in 0.7.0** · Use `container_user: image` for images that expect root.

### L15. Notifications are plain text
**Severity: Low** · Temporary failures are tried three times, but there is no queue. No HTML e-mail, no Matrix formatting.

### L16. No update command
**Severity: Low** · Update by running the installer again. It prints, but doesn't change, your PATH. Offline installs:
[tips.md](tips.md#install-without-internet).

### L17. The TUI can't edit baselines
**Severity: Low** · Its live log keeps the last 5,000 lines (full logs are in the artifact).

### L18. 100% coverage is enforced on Linux only
**Severity: Low** · Gatewright's own tests cover every line and branch, and the self-pipeline and CI fail
below 100%, but only on Linux, the reference platform. Windows and macOS run other OS branches, so
there coverage is reported, not enforced. The few lines that can't run on Linux are listed in
[testing.md](testing.md#pragmas-only-for-code-the-test-os-cant-run).

## Planned

| Version | Planned |
|---|---|
| 0.8 | Scheduled runs, multi-project support, remote agents over SSH |
| 0.9 | Plugin system, trend dashboard, inline PR annotations (Checks API) |
