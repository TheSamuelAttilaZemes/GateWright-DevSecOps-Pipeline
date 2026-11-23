# Extending Gatewright

Recipes for the changes people make most often. Each one lists the files to touch and the test to add.
Read [architecture.md](architecture.md) first for the big picture.

- [Support a new tool (parser)](#support-a-new-tool-parser)
- [Add a friendlier explanation for a rule](#add-a-friendlier-explanation-for-a-rule)
- [Recognise a new infrastructure error](#recognise-a-new-infrastructure-error)
- [Add a metric for policies](#add-a-metric-for-policies)
- [Add a report format](#add-a-report-format)
- [Add a CLI command](#add-a-cli-command)
- [Add a key to the terminal UI](#add-a-key-to-the-terminal-ui)
- [Add a project template](#add-a-project-template)
- [Support another Linux distribution](#support-another-linux-distribution)
- [Read secrets from another store](#read-secrets-from-another-store)
- [Build your own front-end](#build-your-own-front-end)
- [Change colours and symbols](#change-colours-and-symbols)
- [Add a git host](#add-a-git-host)
- [Add a notification channel](#add-a-notification-channel)

## Support a new tool (parser)

**First check whether you need code at all.** If the tool can write SARIF (`--format sarif`; semgrep,
trivy, hadolint, checkov, CodeQL and many more can), use `parser: sarif`. If it can write JUnit XML,
use `parser: junit`. If it only has an exit code, `generic` works.

Otherwise, add a parser in `gatewright/errors.py`:

```python
@parser("shellcheck")
def parse_shellcheck(job, stdout, stderr, code, root, report):
    """`shellcheck -f json1`."""
    data = _json(report or stdout)          # raises ParseError on garbage -> reported as a tool crash
    errs = []
    for c in data.get("comments", []):
        rule = f"SC{c['code']}"
        file, line = c["file"], c["line"]
        human, hint = describe("shellcheck", rule, c["message"], relpath(file, root), line,
                               f"See https://www.shellcheck.net/wiki/{rule}")
        sev = {"error": "high", "warning": "medium"}.get(c["level"], "low")
        errs.append(JobError(job.id, "lint", sev, human, hint, raw=c["message"],
                             file=file, line=line, rule=rule, tool="shellcheck"))
    return errs, {"lint.issues": len(errs)}
```

Rules for parsers:

- Return `(list_of_JobError, metrics_dict)`. Don't set snippets or fingerprints: `finalize()` does that.
- Raise `ParseError` when the output can't be read. Gatewright then reports a tool crash, never a silent pass.
- Use `describe()` so catalog texts are picked up automatically.
- Pick a `category` from `CODE_CATEGORIES` in `models.py` (or add one there).

Then use it in a job: `parser: shellcheck`, or `parser: {type: shellcheck, file: "$GATEWRIGHT_OUT/sc.json"}`.

**Test:** capture real output from the tool into `tests/test_parsers.py` and assert file, line,
severity and wording. Use real output, not hand-written JSON: field names change between versions.
Also add an install hint to `INSTALL_HINTS` in `doctor.py`, and to `TOOL_PACKAGES` in
`platform_info.py` if distributions package it.

## Add a friendlier explanation for a rule

This is the easiest contribution. Add an entry to `CATALOG` in `gatewright/errors.py`:

```python
("bandit", "B324"): ("A weak hash (MD5/SHA1) is used in {loc}.",
                     "Use hashlib.sha256, or pass usedforsecurity=False if it is not security related."),
```

`{loc}` becomes "`file` line N". Write for someone who has never seen the rule: say what is wrong,
why it matters, and what to do.

## Recognise a new infrastructure error

Add a pattern to `INFRA_PATTERNS` in `gatewright/errors.py`:

```python
(re.compile(r"x509: certificate signed by unknown authority", re.I),
 "infra", "The job could not verify a TLS certificate (often a corporate proxy).",
 "Point the tool at your CA bundle (e.g. SSL_CERT_FILE) or fix the proxy configuration."),
```

The category decides behaviour: `infra` and `timeout` are retried, `tool_missing` turns into
`skipped` for optional jobs, and every INFRA category is excluded from findings and baselines.
`{m1}` in the texts is replaced with the first captured group.
Add a case to `test_infra_classification` in `tests/test_parsers.py`.

## Add a metric for policies

Return it from a parser's metrics dict, e.g. `{"sbom.components": 412}`. That's all:
`policy.metrics_for()` sums counters across jobs and keeps the last value for metrics ending in
`percent`. Then use it in `pipeline.yml`:

```yaml
policy:
  - {id: sbom-not-empty, gates: [build], metric: sbom.components, op: ">", value: 0}
```

Document it in the metrics table in [configuration.md](configuration.md#policy).

## Add a report format

In `gatewright/reports.py`, write a function from `RunResult` to text and register it in `write_reports()`:

```python
def export_csv(run: RunResult) -> str:
    rows = ["gate,job,severity,file,line,rule,message"]
    for g in run.gates:
        for j in g.jobs:
            for e in j.active_errors:
                rows.append(",".join(f'"{x}"' for x in (g.gate, j.job_id, e.severity, e.file or "",
                                                         e.line or "", e.rule or "", e.human.replace('"', "'"))))
    return "\n".join(rows) + "\n"

# in write_reports():
    if "csv" in formats:
        p = out_dir / "findings.csv"
        p.write_text(export_csv(run), encoding="utf-8")
        written.append(str(p))
```

Add `csv` to the `--report`/`--format` help texts in `cli.py`, and add a test in `tests/test_parsers.py`
(`test_exports`). Always write files with `encoding="utf-8"`.

## Add a CLI command

In `gatewright/cli.py`:

```python
def cmd_clean(args):
    """`gatewright clean --keep N`: delete old run folders."""
    from .history import History
    ...
    return 0

# in build_parser():
    s = sub.add_parser("clean", help="delete old runs")
    s.add_argument("--keep", type=int, default=20)
    s.set_defaults(fn=cmd_clean)
```

Return `0` for success, `1` for "the check failed", and `2` for usage/config errors. Put real logic in
a module (not in `cli.py`) so the TUI can reuse it.

## Add a key to the terminal UI

In `gatewright/tui.py`, add a `Binding` and an `action_` method:

```python
BINDINGS = [..., Binding("v", "verify", "Verify")]

def action_verify(self) -> None:
    """`v`: verify the last run's checksums."""
    from .artifacts import verify
    problems = verify(Path(self.last_run.artifact_dir)) if self.last_run else ["no run yet"]
    self.notify("Artifact intact" if not problems else "\n".join(problems),
                severity="information" if not problems else "error")
```

Never call engine methods that take long on the UI thread. Use a `@work(thread=True)` worker, as
`_worker` does, and update widgets through `call_from_thread`. Test with Textual's `App.run_test()`
pilot (see how `tests` drive the app headlessly).

## Add a project template

In `gatewright/templates.py`, add `GATES`, `POLICY` and a `{filename: yaml}` dict, register them in
`TEMPLATES`, teach `detect_template()` how to recognise the project, and add the name to the
`--template` choices in `cli.py`. `tests/test_config.py::test_init_templates_are_valid` loads every
template, so a broken template fails the tests. Keep commands portable (see [windows.md](windows.md#writing-job-commands-that-work-everywhere)).

## Support another Linux distribution

Four places must agree, and a test checks that they do:

1. `install.sh` → `family_of()`: add the distribution's `ID` to the right family, or add a new family.
2. `install.sh` → `packages_for()` / `pm_install()`: only for a **new family**: package names and
   the install command.
3. `gatewright/platform_info.py` → `FAMILY_BY_ID` (and `PM_BY_FAMILY` / `PM_INSTALL` for a new family).
4. `tests/fixtures/os-release/<distro>`: a copy of the distribution's real `/etc/os-release`, plus an
   entry in `EXPECTED` in `tests/test_install.py`.

`test_family_tables_in_sync` fails if steps 1 and 3 disagree. Run
`sh install.sh --dry-run --os-release tests/fixtures/os-release/<distro>` to see the result. Ideally also
run a real install in a container of that distribution and mention it in your pull request.

## Read secrets from another store

`SecretStore.get()` in `gatewright/secrets.py` defines the lookup order. To add e.g. `pass` or HashiCorp
Vault, add a lookup step there and call `self._used.add(value)` for every value you return, so
it gets masked. Never log the value, and never put it on a command line (`ps` shows command lines
to other users).

## Build your own front-end

Anything that can call Python can drive Gatewright:

```python
from gatewright.config import load_pipeline
from gatewright.engine import Engine, Selection

def on_event(kind, data):
    if kind == "job_end":
        r = data["result"]
        print(f"{r.job_id}: {r.status} ({len(r.active_errors)} issues)")

engine = Engine(load_pipeline("path/to/project"), on_event)
run = engine.run(Selection(gates=["commit"], reports=["md", "sarif"]))
print(run.status, run.artifact_dir)
```

Events arrive on worker threads. See the event list in [architecture.md](architecture.md#events).
To cancel, call `engine.stop.set()`.

## Change colours and symbols

Terminal styles, symbols and labels live in `STYLE`, `SYMBOL`, `LABEL` and `SEV_STYLE` in
`gatewright/render.py`. They are shared by the CLI and the TUI. Report styling is the `CSS` string in
`gatewright/reports.py`, and Markdown icons are `ICON`/`SEV_ICON` there.

## Add a git host

Implement `VCSProvider` (`gatewright/vcs/base.py`) in a new module, e.g. `vcs/bitbucket.py`:

| Method | Must do |
|---|---|
| `auth_headers()` | HTTP headers that authenticate API calls |
| `list_events(state)` | Return a `VCSEvent` for every watched branch head and open pull request. Use `cached_get()` for list endpoints, so ETags make unchanged polls cheap |
| `set_status(sha, context, state, description, target_url)` | Map Gatewright's `pending/running/success/failure/error` to the host's names |
| `comment(pr, body)` | Comment on a pull request |
| `parse_webhook(headers, body, secret)` | **Verify the signature first** (raise `VCSError` if wrong, using `hmac.compare_digest`), then return events |
| `default_clone_url()` | HTTPS clone URL |
| `fetch_refspec(event)` | Ref that contains the PR head (e.g. `refs/pull/N/head`) |
| `git_auth_header()` | `Authorization` header for `git fetch` (passed via the environment, never argv) |

Set `from_fork=True` for pull requests whose source repository differs from the target: the trust
rules depend on it. Register the class in `PROVIDERS` in `vcs/__init__.py`, add the name to the
`provider` enum in `config.py`'s `VCS_SCHEMA`, and add the host to `tests/fakehost.py` (follow the
host's REST documentation) so `test_watch.py` can run the whole flow against it. Document the token
permissions in [git-integration.md](git-integration.md#host-specific-setup).

## Add a notification channel

In `gatewright/notify.py`, add a method `_send_<type>(self, ch, event, title, text, data, priority)` that
delivers the message and raises on failure (the caller turns exceptions into warnings). Read tokens
with `self._secret(ch, "<key>_secret")` so they come from the secret store. Then add the type to
`CHANNEL_SCHEMA` in `config.py` (plus any new keys), document it in
[notifications.md](notifications.md), and test it against a local server like the tests in
`tests/test_release_notify.py`.
