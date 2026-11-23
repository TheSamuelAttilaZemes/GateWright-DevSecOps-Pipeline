# Errors, reports and artifacts

## How Gatewright explains a problem

Every problem a job finds becomes a `JobError` (see `gatewright/models.py`):

| Field | Example |
|---|---|
| `severity` | `critical` · `high` · `medium` · `low` · `info` |
| `category` | `finding`, `lint`, `test_failure`, `vulnerability`, `secret`, `job_failure`, `policy` (CODE) · `tool_missing`, `timeout`, `resource`, `config`, `crash`, `infra` (INFRA) |
| `human` | "A subprocess is started with shell=True in `app/config.py` line 9. If any argument contains user input, it can be used to inject shell commands." |
| `hint` | "Pass the command as a list and drop shell=True." |
| `file`, `line`, `snippet` | `app/config.py`, `9`, the lines around it |
| `rule`, `tool` | `B602`, `bandit` |
| `fingerprint` | `71cb79a1cf65e400`, a stable id built from the tool, rule, file and **the line's text** (not its number), so it survives code moving up or down |
| `suppressed`, `suppression_reason` | set by the baseline |

### CODE vs INFRA

| | CODE (red) | INFRA (orange) |
|---|---|---|
| Meaning | Your code is wrong | The pipeline or machine is wrong |
| Examples | failing test, hardcoded password, vulnerable dependency, lint error | tool not installed, timeout, out of memory, Docker not running, network down, tool crashed, missing secret |
| Job status | `failed` | `error` |
| Retried? | never | yes, if the job sets `retries:` (timeouts and infra failures) |
| Can be suppressed? | yes (baseline) | no |
| Counts in `findings.*` metrics? | yes | no (shows in `jobs.error`) |

Gatewright checks for INFRA causes **before** parsing results. So "pytest is not installed" is never
reported as "tests failed", and a tool that crashed is never reported as "no findings".

### Where the text comes from

1. **The catalog** (`CATALOG` in `gatewright/errors.py`): hand-written explanations for common rules
   (bandit B105/B602/B608..., ruff F401/F821/E722...).
2. **The tool's own message**, with the location added.
3. **The generic parser**: for unknown tools, the exit code plus the last output line, and the first
   `file:line` it can find (Python tracebacks, compiler-style messages).

Adding entries to the catalog is the easiest way to contribute. See [extending.md](extending.md#add-a-friendlier-explanation-for-a-rule).

### The red markup

When a finding has a file and line, Gatewright reads the file and shows a few lines around it, with the
failing line highlighted in red:

- in the terminal and TUI (`gatewright show`, the Errors tab), via Rich (`gatewright/render.py`)
- in HTML/PDF reports, as a code table with the line in red
- in Markdown, as a fenced code block with `>>` marking the line

Paths reported from inside containers (`/work/...`) are mapped back to your files.

## Baselines

Old findings shouldn't block new work, but they shouldn't be forgotten either. A **suppression**
accepts a finding with a reason and an expiry date:

```yaml
# .gatewright/baseline.yml
suppressions:
  - fingerprint: 3245a3156b9846f6        # exact finding (shown by `gatewright show`)
    reason: "Test fixture, not a real password"
    expires: 2027-01-31
    owner: alice
  - rule: B101                           # or: a rule, optionally limited by file glob and job
    file: "tests/*"
    reason: "asserts are fine in tests"
    expires: 2027-06-30
```

- `reason` is required, and an entry without one is ignored with a warning.
- When `expires` passes, the finding is **active again**, and the run warns that the suppression expired.
- INFRA errors can't be suppressed.
- `gatewright baseline create [run] --days 90 --reason "..."` adds every current finding. Review the
  file afterwards and give each entry a real reason.
- Suppressed findings are listed separately in every report and marked as suppressed in SARIF.

## Reports

Every run writes reports into its artifact folder. Choose formats with `gatewright run --report ...`
(default `md,html,sarif,junit`), or later with `gatewright report [run] -f ...`. Reports made later go to
`.gatewright/reports/<run>/` (or `-o DIR`), never into the artifact: that would break its checksums
and signature.

A run's status is one of `passed`, `warn` (only warnings, or only advisory gates failed), `failed`,
`skipped` (nothing matched the selection) and `cancelled` (stopped before it finished; exit code 130).
Only `passed` and `warn` runs can be promoted to an environment.

| File | Format | For |
|---|---|---|
| `report.md` | Markdown | Summary, all gates, every error with code, policy, suppressed findings, tool versions. Readable on GitHub/GitLab. |
| `gate-<id>.md` | Markdown | One gate only, to attach to a change or ticket |
| `report.html` | HTML | Self-contained, styled, red error boxes and code |
| `report.pdf` | PDF | Same as the HTML (needs WeasyPrint + Pango) |
| `results.sarif` | SARIF 2.1 | Code scanning UIs (GitHub/GitLab code scanning, VS Code SARIF viewer, DefectDojo) |
| `junit.xml` | JUnit XML | Test dashboards: gates as suites, jobs as test cases |

The summary also includes the **diff against the previous run**: new findings, fixed findings,
unchanged ones.

## Artifacts

```
.gatewright/runs/<run-id>/
  manifest.json        everything about the run (see below)
  logs/<job>.log       full output of every job, secrets masked
  out/<job>/           files the job wrote to $GATEWRIGHT_OUT, plus its declared outputs
  report.md, gate-*.md, report.html, report.pdf, results.sarif, junit.xml
  checksums.sha256     SHA-256 of every file
  checksums.sha256.sig SSH signature over checksums.sha256 (if `signing:` is configured)
  signature.json       who signed, key fingerprint, when
.gatewright/runs/gatewright-run-<run-id>.tar.gz
```

`manifest.json` holds: Gatewright version, run id, start/end time, status, commit, branch, whether there
were uncommitted changes, profile, hardware (CPU, RAM, distribution), the selection (gates,
categories, jobs, mode), every gate/job/error, policy results, warnings, and every tool's version.
Secret values never appear in it.

`gatewright verify [run]` re-computes every checksum and reports modified, missing or unexpected files.
Checksums alone detect accidental change. With **signing** (v0.6), a signature over the checksums also
proves the artifact came from a trusted key and wasn't changed since. See [releases.md](releases.md#signing-artifacts).

## What to do with artifacts

DevOps courses often stop at "the pipeline is green". This is what happens next:

1. **Store them.** Keep the `.tar.gz` (or the folder) somewhere durable: a shared folder, an
   S3-compatible store (MinIO, Garage), or your git host's release assets. Name them by version and commit.
2. **Promote the same artifact.** What passed `test` is what goes to `staging` and then production.
   Never rebuild between stages. `gatewright promote --to <env>` does exactly this, and environment
   rules (`requires`, `after`, `require_signature`, `approval`) enforce the order. See [releases.md](releases.md).
3. **Deploy from it.** Your `deploy` command reads the built package or image reference from
   `$GATEWRIGHT_ARTIFACT`, not from a fresh build.
4. **Keep an audit trail.** Reports plus manifest answer "what exactly was tested before release
   X, with which tool versions, on which commit". That matters for incident reviews, compliance
   (ISO 27001, SOC 2) and customer questions.
5. **Roll back.** Because every released artifact is kept, going back is `gatewright rollback <env>`.
6. **Feed other tools.** Upload `results.sarif` to a code-scanning UI and `junit.xml` to a test
   dashboard. Parse `manifest.json` for your own metrics.
7. **Clean up.** `gatewright clean` deletes old runs. A simple policy: keep the last
   20 runs plus everything that was released.
