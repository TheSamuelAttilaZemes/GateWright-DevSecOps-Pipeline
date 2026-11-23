# Configuration reference

A Gatewright project is configured by files in `.gatewright/`:

| File | Purpose | Commit it? |
|---|---|---|
| `.gatewright/pipeline.yml` | gates, profile, limits, policy (and optionally jobs) | yes |
| `.gatewright/jobs/*.yml` | job definitions (the "preloaded jobs") | yes |
| `.gatewright/baseline.yml` | accepted findings, see [errors-and-reports.md](errors-and-reports.md#baselines) | yes |
| `.gatewright/secrets.env` | local secrets, see [Secrets](#secrets) | **never** |

Gatewright reads YAML with **YAML 1.2 booleans**: only `true`/`false` are booleans, so `on`, `off`, `yes`
and `no` stay text (write `cache: false`, not `cache: no`).

Run `gatewright validate` after editing. It reports **every** problem at once, with file and location:

```
✗ The pipeline configuration has problems:
  • .gatewright/jobs/commit.yml:jobs[1]: category: must be one of ['dev', 'sec', 'ops'], got 'qa'
  • .gatewright/jobs/test.yml: job 'unit' needs unknown job 'compile2'
```

## pipeline.yml

```yaml
version: 1
name: my-service            # shown in reports (default: folder name)
profile: auto               # auto | laptop | server
shell: auto                 # default shell for all jobs: auto | sh | bash | cmd | powershell
limits:
  max_parallel: 4           # never run more jobs at once than this
  max_cpu_percent: 80       # don't START new jobs while system CPU is above this
  max_mem_percent: 80       # don't START new jobs while system RAM is above this
  kill_mem_percent: 92      # watchdog: stop the newest job above this RAM usage
gates:
  - id: commit
    mode: enforced
    description: Fast checks
  - id: test
    coverage: 80            # this gate needs >= 80% test coverage
  - id: staging
    mode: advisory
    enabled: false
    requires: [test]
coverage: 75                # the whole run needs >= 75% (optional)
policy:
  - id: no-critical
    metric: findings.critical
    op: "=="
    value: 0
jobs: []                    # optional: jobs can also live here instead of .gatewright/jobs/
```

| Key | Type | Default | Description |
|---|---|---|---|
| `version` | integer | | Config format version. Currently `1`. |
| `name` | string | folder name | Pipeline name in reports and the TUI. |
| `profile` | `auto` \| `laptop` \| `server` | `auto` | Resource profile. `auto`: a machine with a battery is a laptop; 8+ CPUs and 16 GB+ RAM without a battery is a server. See [resources.md](resources.md). |
| `shell` | `auto` \| `sh` \| `bash` \| `cmd` \| `powershell` | `auto` | Default shell for jobs. `auto` = `/bin/sh` on Linux/macOS, Git Bash (else cmd) on Windows. |
| `limits` | mapping | per profile | Overrides of the profile's limits (below). |
| `gates` | list (required, ≥1) | | Gates in execution order. |
| `coverage` | number 0–100, or mapping | | Minimum test coverage for the **whole run**. See [coverage](#coverage). |
| `policy` | list | `[]` | Policy rules. |
| `keep_runs` | integer ≥ 1 | keep all | After each run, delete the files of all but the newest N runs. Deployed runs are always kept. Same as `gatewright clean --keep N`. |
| `vcs` | mapping | | Git host integration (v0.5). See [vcs](#vcs). |
| `signing` | mapping | | Artifact signing (v0.6). See [signing](#signing). |
| `environments` | mapping | | Deploy targets (v0.6). See [environments](#environments). |
| `notify` | mapping | | Notifications (v0.6). See [notify](#notify). |
| `jobs` | list | `[]` | Jobs defined inline (same format as job files). |

### limits

| Key | Range | Laptop default | Server default | Meaning |
|---|---|---|---|---|
| `max_parallel` | ≥ 1 | half the CPUs (halved again on battery) | CPUs − 1 | Maximum jobs running at the same time. |
| `max_cpu_percent` | 10–100 | 80 | 90 | New jobs wait while system CPU is above this. |
| `max_mem_percent` | 10–100 | 80 | 85 | New jobs wait while system RAM is above this. |
| `kill_mem_percent` | 10–100 | 92 | 95 | The watchdog stops the newest job above this RAM usage. |

A job always starts if nothing else is running, so a job that is too big for the budget still runs
(alone) instead of waiting forever.

### gates

| Key | Type | Default | Description |
|---|---|---|---|
| `id` | string (`A-Z a-z 0-9 _ . -`) | required | Gate name, used in jobs (`gate:`), CLI (`-g`) and reports. |
| `mode` | `enforced` \| `advisory` | `enforced` | **enforced**: a failure blocks every later gate. **advisory**: the failure is recorded and the pipeline continues. Override for one run with `gatewright run --mode advisory`. |
| `enabled` | boolean | `true` | Disabled gates only run when selected explicitly (`gatewright run -g staging`). |
| `description` | string | | Shown in docs/reports. |
| `requires` | list of gate ids | `[]` | Gates that must have **passed for the same commit**, either earlier in this run or in an earlier run (looked up in the history). Lets you run `gatewright run -g staging` on its own, safely. Needs git. Only gates listed *before* this one are allowed. |
| `coverage` | number 0–100, or mapping | | Minimum test coverage for **this gate**. See [coverage](#coverage). |

### coverage

How much test coverage your project needs is your decision, so Gatewright sets no default. Set a
minimum per gate, for the whole run, or both:

```yaml
coverage: 75                 # whole run: the lowest coverage any job reported must be >= 75
gates:
  - id: test
    coverage: 85             # the test gate: its coverage jobs must report >= 85
  - id: integration
    coverage: {min: 60, missing: ignore}
```

| Key | Type | Default | Description |
|---|---|---|---|
| `coverage` | number 0–100 | | Short form: the minimum, with `missing: fail`. |
| `min` | number 0–100 | required | The minimum `coverage.percent`. |
| `missing` | `fail` \| `ignore` | `fail` | What happens when **no job measured coverage** (no job with `parser: coverage`, or its tool was skipped). `fail`: the gate (or run) fails, so a coverage requirement never passes silently. `ignore`: the rule is reported as skipped. |

- The number comes from jobs with `parser: coverage` (coverage.py JSON). If several jobs report
  coverage, the **lowest** counts.
- A failed minimum fails the gate like any other policy rule: an enforced gate stops the pipeline,
  an advisory gate only records it. A failed run-level minimum fails the run.
- When you run only part of the pipeline (`-j`, `-c`, or not all enabled gates, e.g. a pre-push hook
  that runs only `commit`), a minimum that has no measurement is *skipped*, not failed.
- A cancelled run gets no coverage verdict.
- Override for one run with `gatewright run --coverage 85` (whole run) or `--coverage test=90` (one gate).
  Repeat the flag to set several. `gatewright plan` shows every minimum, and warns when a gate has a
  minimum but none of its jobs measures coverage.
- In reports and the history the minimums appear as policy rules `min-coverage` (run) and
  `min-coverage-<gate>`.

### policy

Policy rules compare a metric to a number. No code is evaluated.

| Key | Type | Description |
|---|---|---|
| `id` | string | Rule name in reports. |
| `metric` | string | See the metrics table below. |
| `op` | `==` `!=` `<` `<=` `>` `>=` | Comparison. |
| `value` | number | Threshold. |
| `gates` | list | Evaluate after each of these gates, using that gate's jobs. **Empty = evaluate once at the end, over the whole run.** |
| `severity` | `critical` … `info` | For reports. |
| `description` | string | Shown next to the result. |

A failed rule fails its gate (respecting the gate's mode), or the whole run for run-level rules.
If no job produced the metric, the rule is reported as *skipped*, not failed.

| Metric | Meaning |
|---|---|
| `findings.critical` / `.high` / `.medium` / `.low` / `.info` | Unsuppressed code findings of that severity |
| `findings.total` | All unsuppressed code findings |
| `findings.suppressed` | Findings suppressed by the baseline |
| `jobs.total`, `jobs.failed`, `jobs.error` | Job counts |
| `tests.total`, `tests.failed`, `tests.skipped`, `tests.passed` | From the `junit` parser |
| `coverage.percent` | From the `coverage` parser. Several jobs: the lowest. (Simpler: the [`coverage`](#coverage) setting.) |
| `lint.issues` | From the `ruff` parser |
| `deps.vulnerable` | From the `pip-audit` parser |

Infra errors (tool missing, timeout...) never count as findings. They show up through `jobs.error`.

### vcs

Git host integration (v0.5). Guide: [git-integration.md](git-integration.md).

```yaml
vcs:
  provider: github
  repo: you/app
  mode: auto-trusted
  trusted: [you, teammate]
  branches: [main, "release/*"]
  webhook: {port: 8787, secret: VCS_WEBHOOK_SECRET}
```

| Key | Type | Default | Description |
|---|---|---|---|
| `provider` | `github` \| `gitea` \| `forgejo` \| `gitlab` | required | The git host's API. |
| `repo` | string | required | `owner/name` (GitLab: the full project path). |
| `api_url` | URL | per provider | API base. Required for Gitea/Forgejo (e.g. `https://codeberg.org/api/v1`). GitHub Enterprise: `https://host/api/v3`. |
| `clone_url` | URL or path | derived from `api_url` | Where `git fetch` gets commits from. |
| `token_secret` | secret name | `VCS_TOKEN` | Secret holding the API token. |
| `branches` | list of globs | `[main, master]` | Branches whose new commits are queued. |
| `pull_requests` | boolean | `true` | Also queue open pull/merge requests. |
| `mode` | `manual` \| `auto-trusted` \| `auto` | `manual` | What runs without `gatewright approve`. Pull requests from forks always need approval. |
| `trusted` | list of user names | `[]` | Authors whose non-fork pull requests are trusted. |
| `gates` | list of gate ids | enabled gates | Gates to run for queued commits. |
| `poll_interval` | seconds ≥ 10 | `60` | How often to poll the host. |
| `untrusted` | `run` \| `containers` \| `refuse` | `run` | What an **approved** untrusted commit (e.g. a fork's pull request) may do. `containers`: only run if every job has `image:` and `network: false`. `refuse`: never run. |
| `parallel` | 1–16 | `1` | Commits run at the same time. They share the machine's job slots. |
| `fetch_depth` | integer ≥ 0 | `50` | Commits of history to fetch. `0` = all (for tools that need the full history). |
| `submodules` | boolean | `false` | Also fetch git submodules (trusted commits only). |
| `lfs` | boolean | `false` | Also fetch Git LFS files (trusted commits only; needs git-lfs). |
| `webhook` | mapping | none | Also listen for webhooks: `host` (default `127.0.0.1`), `port` (1-65535, default `8787`), `path` (default `/hook`), `secret` (secret name, default `VCS_WEBHOOK_SECRET`). |
| `report_url` | string | none | Link put on statuses and PR comments; `{run_id}` is replaced, e.g. `https://ci.example/runs/{run_id}/report.html`. |
| `comment` | boolean | `true` | Post a summary comment on pull requests. |
| `status_prefix` | string | `gatewright` | Status context prefix: `<prefix>/<gate>`. |

### signing

Artifact signing with SSH signatures (v0.6). Guide: [releases.md](releases.md#signing-artifacts).

| Key | Type | Default | Description |
|---|---|---|---|
| `key` | path | `~/.config/gatewright/signing_key` (Windows `%APPDATA%\gatewright\signing_key`) | Private key used to sign (create with `gatewright keygen`). |
| `identity` | string | required to sign | Who signs, e.g. an e-mail address. Must match the key's line in `allowed_signers`. |
| `allowed_signers` | path | `.gatewright/allowed_signers` | OpenSSH allowed-signers file: the public keys you trust. Commit it. |
| `auto` | boolean | `true` | Sign every run when it finishes. |
| `required` | boolean | `false` | Signing problems become errors in `doctor`; `promote` and `verify` require a valid signature. |

### environments

Deploy targets (v0.6). Guide: [releases.md](releases.md#environments).

```yaml
environments:
  production:
    deploy: ./scripts/deploy.sh production
    verify: ./scripts/health.sh production
    after: [staging]
    approval: true
```

| Key | Type | Default | Description |
|---|---|---|---|
| `deploy` | command | required | Deploys `$GATEWRIGHT_ARTIFACT`. Runs like a job (logs, timeout, secrets, errors). |
| `verify` | command | none | Health check after deploying. Failure triggers `auto_rollback`. |
| `requires` | list of gate ids | `[]` | Gates that must have passed in the run. |
| `after` | list of environments | `[]` | The run must already be deployed successfully to these. |
| `require_signature` | boolean | `false` | The artifact must carry a valid, trusted signature. |
| `auto_rollback` | boolean | `false` | If `verify` fails, redeploy the previously live artifact. |
| `approval` | boolean | `false` | Ask for confirmation (or require `--yes`). |
| `timeout` | seconds | `1800` | For `deploy` and `verify` each. |
| `secrets` | list | `[]` | Secrets passed to `deploy`/`verify` (masked in logs). |
| `env` | mapping | `{}` | Extra environment variables. |
| `release_notes` | boolean | `false` | Write `release-notes.md` into each deployment's folder. |
| `shell` | `auto` \| `sh` \| `bash` \| `cmd` \| `powershell` | `auto` | Shell for `deploy`/`verify`. |

### notify

Notifications (v0.6). Guide: [notifications.md](notifications.md).

| Key | Type | Description |
|---|---|---|
| `on` | list of events | Default events for every channel (see below). Default: `run_failed`, `approval_needed`, `deploy_failed`, `rolled_back`. |
| `channels` | list | One entry per destination. |

Events: `run_passed`, `run_warn`, `run_failed`, `run_cancelled`, `run_skipped`, `gate_failed`, `approval_needed`, `deployed`,
`deploy_failed`, `rolled_back`.

Channel settings:

| Key | For | Description |
|---|---|---|
| `type` | all | `ntfy`, `webhook`, `email` or `matrix`. |
| `on` | all | Events for this channel (overrides the top-level `on`). |
| `url` | ntfy, webhook | Topic URL / webhook URL. |
| `token_secret` | ntfy, matrix | Secret holding an access token. |
| `secret` | webhook | Secret used to sign the body (`X-Gatewright-Signature: sha256=...`). |
| `smtp` | email | `host:port` (587 = STARTTLS, 465 = TLS). |
| `from`, `to` | email | Sender, and list of recipients. |
| `username_secret`, `password_secret` | email | Secrets for SMTP login. |
| `starttls` | email | Use STARTTLS on non-465 ports (default `true`). |
| `homeserver`, `room` | matrix | Homeserver URL and room ID (`!abc:example.org`). |

## Job files

A file in `.gatewright/jobs/` holds either one job (a mapping) or a list under `jobs:`.
Files are loaded in alphabetical order. Job ids must be unique across all files.

```yaml
jobs:
  - id: sast
    name: SAST (bandit)
    gate: commit
    category: sec
    run: bandit -r src -f json -q
    parser: bandit
    needs: [lint]
    resources: {cpu: 1, mem: 512MB}
    timeout: 600
    retries: 0
    optional: false
    fail_on: medium
    inputs: ["src/**/*.py"]
    outputs: []
    secrets: []
    env: {BANDIT_CONFIG: .bandit}
    cache: true
    tool: bandit
    shell: auto
    image: null
    network: true
```

| Key | Type | Default | Description |
|---|---|---|---|
| `id` | string (`A-Z a-z 0-9 _ . -`) | required | Unique job id. |
| `gate` | gate id | required | The gate this job belongs to. |
| `run` | string | required | The command. Runs in the project root, in the job's `shell`. |
| `name` | string | `id` | Display name. |
| `category` | `dev` \| `sec` \| `ops` | `dev` | Used to filter (`gatewright run -c sec`, TUI checkboxes). |
| `needs` | list of job ids | `[]` | Must finish OK first (same or earlier gate). If one fails, this job is `blocked`. |
| `parser` | name, or `{type, file}` | `generic` | How to read the result: `generic`, `ruff`, `bandit`, `junit` (alias `pytest`), `sarif`, `gitleaks`, `pip-audit`, `mypy`, `coverage`. Use `file:` when the tool writes a report file (supports `$GATEWRIGHT_OUT`). |
| `resources` | mapping | | What the job needs, for the scheduler: `cpu` and `mem` below. |
| `resources.cpu` | number > 0 | `1` | CPU units the job needs (scheduler budget, and `--cpus` in containers). |
| `resources.mem` | `256MB`, `1.5GB`, number (MB) | `256MB` | Memory the job needs (budget, and `--memory` in containers). |
| `timeout` | seconds | `600` | The job is stopped (whole process tree) after this and reported as an INFRA timeout. |
| `retries` | 0–10 | `0` | Extra attempts, **only** for timeouts and infrastructure failures (network errors, Docker unavailable, permission problems), waiting 2s, 4s, 8s… (max 30s) between attempts. A failing test or a finding is never retried. |
| `optional` | boolean | `false` | If the tool is not installed, the job is `skipped` instead of `error`. |
| `fail_on` | `critical` \| `high` \| `medium` \| `low` \| `info` | `low` | Lowest severity that fails the job. Findings below it make the job `warn`. |
| `inputs` | list of globs | all files | Files whose content goes into the cache key. Narrow this for faster caching. |
| `outputs` | list of globs | `[]` | Files copied into the artifact (`out/<job>/`) after the job. |
| `secrets` | list of names | `[]` | Secrets injected as environment variables (see below). |
| `env` | mapping | `{}` | Extra environment variables. |
| `cache` | boolean | `true` | Allow caching. Turn off for jobs with side effects (deploys, network scans). |
| `when_exists` | list of globs | `[]` | Run only if at least one of these files exists (e.g. `[requirements.txt]`). Otherwise the job is `skipped` with a note. |
| `tool` | string | first word of `run` | The program to check for with `doctor`/dry-run and to ask for `--version`. For `python -m X` it is `X`. |
| `shell` | `auto` \| `sh` \| `bash` \| `cmd` \| `powershell` | pipeline `shell` | See [windows.md](windows.md#how-gatewright-runs-commands-on-windows). |
| `image` | string | none | Run inside this container image (`docker run --rm`, project mounted at `/work`, cpu/mem as hard limits). Container jobs are never cached. |
| `network` | boolean | `true` | `false` runs the container with `--network none`. |
| `container_user` | `host` \| `image` | `host` | `host`: the container runs as you (Linux), so files stay yours. `image`: as the image's own user, for images that expect root. |

### Variables available to jobs

| Variable | Value |
|---|---|
| `GATEWRIGHT_OUT` | A per-job output folder inside the artifact. Write reports here. |
| `GATEWRIGHT_RUN_ID` | The run id, e.g. `20261122-074144-2463`. |
| `GATEWRIGHT_JOB`, `GATEWRIGHT_GATE` | The job and gate ids. |
| `NO_COLOR=1`, `PYTHONUNBUFFERED=1`, `PYTHONUTF8=1` | So tool output streams live, is plain text, and is UTF-8. |

Gatewright replaces `$GATEWRIGHT_X`, `${GATEWRIGHT_X}`, `%GATEWRIGHT_X%` and `$env:GATEWRIGHT_X` in `run:` and `parser.file`
itself, so any of those spellings works in any shell.

## Secrets

A job only gets the secrets it lists in `secrets:`. For a secret named `API_TOKEN`, Gatewright looks in:

1. the environment variable `GATEWRIGHT_SECRET_API_TOKEN`
2. the environment variable `API_TOKEN`
3. `.gatewright/secrets.env` (`API_TOKEN=value` lines; on Linux/macOS Gatewright warns if other users can read it)

If one is missing, the job fails with a config error naming it. Every resolved value of 4 or more
characters is replaced by `***` in logs, errors, reports and the manifest. Container jobs receive
secrets as `-e NAME` (value inherited, never on a command line).
**Limits:** masking is exact-match. A secret that a tool prints base64-encoded or split across
lines is not masked.

## Environment variables read by Gatewright

| Variable | Effect |
|---|---|
| `NO_COLOR` | Plain output without colours. |
| `GATEWRIGHT_SECRET_<NAME>` | Secret value (see above). |
| `GATEWRIGHT_PREFIX`, `GATEWRIGHT_BIN_DIR` | Install locations for `install.sh`. |
| `GATEWRIGHT_TEST_IMAGE` | Container image used by the Docker tests. |
| `GATEWRIGHT_SECRET_VCS_TOKEN` | The git host API token (the default `vcs.token_secret`). |
