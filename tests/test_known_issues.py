# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""One or more regression tests per fixed entry of docs/known-issues.md."""

import base64
import json
import os
import shutil
import smtplib
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from gatewright import cleanup, cli, doctor, hardware, locks, notify, platform_info, runner, signing
from gatewright.baseline import Baseline
from gatewright.config import load_pipeline
from gatewright.deploy import Deployer, DeployError
from gatewright.engine import Engine, Selection
from gatewright.errors import classify_infra, finalize_all
from gatewright.models import Job, JobError
from gatewright.queue import Queue
from gatewright.secrets import SecretStore
from gatewright.vcs import VCSError, VCSEvent
from gatewright.vcs import base as vcs_base
from gatewright.vcs.base import MAX_PAGES, Http, cached_get, rate_limit_end, slim

from .conftest import BASIC_PIPELINE, PY
from .fakehost import FakePR, git
from .test_watch import repo, secrets  # noqa: F401 - fixtures

SIMPLE = "jobs:\n  - {id: a, gate: commit, run: 'echo hi', cache: false}\n"


# ---------------------------------------------------------------- containers
def test_docker_down_with_exit_127_is_not_a_missing_command():
    """Regression: newer docker CLIs exit 127 when the daemon's socket doesn't exist."""
    job = Job(id="x", gate="g", run="echo hi", image="alpine")
    down = "docker: failed to connect to the docker API at unix:///tmp/x.sock: no such file or directory"
    assert "Docker is not running" in classify_infra(job, 127, down, False).human
    missing = classify_infra(job, 127, 'exec: "echo": executable file not found in $PATH', False)
    assert missing.category == "tool_missing" and "container image `alpine`" in missing.human
    assert "not executable" in classify_infra(job, 126, "permission denied", False).human
    assert "on this machine" in classify_infra(Job(id="x", gate="g", run="zz"), 127, down, False).human


def test_container_job_without_docker_names_docker(project, monkeypatch):
    """Regression: without docker the image was blamed for not containing the job's command."""
    root = project(BASIC_PIPELINE, "jobs:\n  - {id: x, gate: commit, image: 'alpine', run: 'echo hi'}\n")
    monkeypatch.setattr(runner, "tool_available", lambda job, root=None: False)
    e = Engine(load_pipeline(root)).run(Selection(reports=["md"])).all_jobs()[0].errors[0]
    assert e.category == "tool_missing" and "`docker`" in e.human and "alpine" not in e.human


def test_container_output_folder_outside_the_checkout(tmp_path, monkeypatch):
    """Regression: in watcher runs $GATEWRIGHT_OUT lies outside the mounted checkout."""
    job = Job(id="x", gate="g", run="echo hi", image="alpine")
    argv, _ = runner._docker_argv(job, str(tmp_path / "ws"), "r", str(tmp_path / "runs" / "o"), [])
    assert f"{tmp_path / 'runs' / 'o'}:/gatewright-out" in argv and "GATEWRIGHT_OUT=/gatewright-out" in argv
    argv, _ = runner._docker_argv(job, str(tmp_path), "r", str(tmp_path / "runs" / "o"), [])
    assert "GATEWRIGHT_OUT=/work/runs/o" in argv and argv.count("-v") == 1

    def other_drive(*a):
        raise ValueError("path is on mount 'D:'")
    monkeypatch.setattr(runner.os.path, "relpath", other_drive)
    assert not runner._inside("D:\\x", "C:\\y")


def test_container_user(tmp_path, monkeypatch):     # L14
    monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(os, "getgid", lambda: 1000, raising=False)
    for who, want in (("host", True), ("image", False)):
        job = Job(id="x", gate="g", run="apk add git", image="alpine", container_user=who)
        assert ("--user" in runner._docker_argv(job, str(tmp_path), "r", str(tmp_path / "o"), [])[0]) is want


# ---------------------------------------------------------------- L1: untrusted commits
@pytest.mark.parametrize("policy, jobs, why", [
    ("refuse", None, "never run"),
    ("containers", None, "these jobs don't: check, uses-secret"),
])
def test_untrusted_commits_are_refused(repo, secrets, policy, jobs, why):  # noqa: F811
    r = repo(extra=f"  untrusted: {policy}")
    r.commit("evil", {"check.py": "open('pwned.txt', 'w')\n"}, branch="evil")
    r.push("evil")
    r.host.add_pr(FakePR(7, "evil", "eve", from_fork=True))
    logs = []
    w = r.watcher(logs)
    w.poll_once()
    q = Queue(str(r.home))
    item = next(i for i in q.list() if i.pr == 7)
    q.approve(item.id, "me")
    assert w.run_approved() == []
    item = q.get(item.id)
    assert item.state == "error" and why in item.note
    trusted = next(i for i in q.list() if i.kind == "push")     # trusted commits are not affected
    q.approve(trusted.id, "me")
    assert len(w.run_approved()) == 1


def test_untrusted_in_containers_is_allowed(repo, secrets):  # noqa: F811
    r = repo(extra="  untrusted: containers")
    w = r.watcher()
    p = load_pipeline(r.home)
    for j in p.jobs:
        j.image, j.network = "alpine", False
    assert w._untrusted_problem(p) == ""


# ---------------------------------------------------------------- L2: encoded secrets
def test_encoded_secrets_are_masked(tmp_path, monkeypatch):
    value = 'p@ss"w/ørd+12?3'
    monkeypatch.setenv("GATEWRIGHT_SECRET_T", value)
    s = SecretStore(str(tmp_path))
    s.get("T")
    raw = value.encode()
    for text in (value, raw.hex(), raw.hex().upper(), urllib.parse.quote(value, safe=""),
                 urllib.parse.quote_plus(value), json.dumps(value), base64.b64encode(raw).decode(),
                 base64.urlsafe_b64encode(raw).decode()):
        assert "***" in s.mask(f"x {text} y"), text
    for before in ("", "u:", "us:", "use:", "user:"):           # inside a Basic auth header, any alignment
        for after in (b"", b"!", b"!!"):
            enc = base64.b64encode(before.encode() + raw + after).decode()
            masked = s.mask(enc)
            assert "***" in masked and len(masked.replace("***", "")) <= len(before) * 4 / 3 + 8, (before, masked)
    assert s.mask("ordinary text") == "ordinary text"


def test_multi_line_and_short_secrets(tmp_path, monkeypatch):
    key = "-----BEGIN KEY-----\nMIIEvQIBADANBgkq\nshort\n-----END KEY-----"
    monkeypatch.setenv("GATEWRIGHT_SECRET_K", key)
    monkeypatch.setenv("GATEWRIGHT_SECRET_PIN", "1234")
    s = SecretStore(str(tmp_path))
    s.get("K"), s.get("PIN"), s.get("K")
    assert s.mask("line MIIEvQIBADANBgkq") == "line ***"          # printed line by line
    assert s.mask("short -----BEGIN KEY-----") == "short -----BEGIN KEY-----"
    assert s.mask("1234 " + "1234".encode().hex()) == "*** 31323334"   # too short for encodings


# ---------------------------------------------------------------- L3: passphrases and ssh-agent
def test_signing_falls_back_to_the_agent(tmp_path, monkeypatch):
    key = tmp_path / "k"
    key.write_text("private, with passphrase")
    (tmp_path / "k.pub").write_text("ssh-ed25519 AAAA")
    (tmp_path / "checksums.sha256").write_text("x")
    used = []

    def fake(args, stdin=None):
        if args[0] == "-l":
            return subprocess.CompletedProcess(args, 1, b"", b"")
        used.append(Path(args[args.index("-f") + 1]).name)
        ok = used[-1].endswith(".pub") and agent
        return subprocess.CompletedProcess(args, 0 if ok else 255, b"", b"incorrect passphrase")
    monkeypatch.setattr(signing, "_run", fake)
    cfg = signing.SigningConfig(key=str(key), identity="me")
    agent = True
    signing.sign(tmp_path, cfg)
    assert used == ["k", "k.pub"]
    agent = False
    with pytest.raises(signing.SigningError, match="incorrect passphrase.*ssh-agent"):
        signing.sign(tmp_path, cfg)
    key.unlink()                                   # the private key lives on a security key
    agent, used[:] = True, []
    signing.sign(tmp_path, cfg)
    assert used == ["k.pub"]


def test_keygen_passphrase_and_timeout(tmp_path, monkeypatch):
    seen = []

    def fake(argv, **kw):
        seen.append(argv)
        (tmp_path / "k").write_text("x")
        (tmp_path / "k.pub").write_text("ssh-ed25519 AAAA me")
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    monkeypatch.setattr(signing, "ssh_keygen", lambda: "ssh-keygen")
    monkeypatch.setattr(signing.subprocess, "run", fake)
    assert "AAAA" in signing.keygen(tmp_path / "k", "me", passphrase=True)
    assert "-N" not in seen[0]                      # ssh-keygen asks for it

    def hangs(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 60)
    monkeypatch.setattr(signing.subprocess, "run", hangs)
    with pytest.raises(signing.SigningError, match="60 seconds"):
        signing._run(["x"])


@pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="needs ssh-keygen")
def test_real_passphrase_key_fails_fast(tmp_path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "correct horse", "-f", str(tmp_path / "k")], check=True)
    (tmp_path / "checksums.sha256").write_text("x")
    t = time.monotonic()
    with pytest.raises(signing.SigningError, match="ssh-agent"):
        signing.sign(tmp_path, signing.SigningConfig(key=str(tmp_path / "k"), identity="me"))
    assert time.monotonic() - t < 30                # never waits for someone to type


# ---------------------------------------------------------------- L4: one deployment at a time
def test_file_lock(tmp_path):
    lock = tmp_path / "sub" / "x.lock"
    with locks.file_lock(lock):
        with pytest.raises(locks.Locked):
            with locks.file_lock(lock):
                pass
    with pytest.raises(ZeroDivisionError):
        with locks.file_lock(lock):
            1 / 0
    with locks.file_lock(lock):                     # released after an error
        pass


def test_second_promote_is_refused(project):
    root = project(BASIC_PIPELINE + "environments:\n  prod/eu: {deploy: 'echo ok'}\n  other: {deploy: 'echo ok'}\n", SIMPLE)
    r = Engine(load_pipeline(root)).run(Selection(reports=["md"]))
    d = Deployer(load_pipeline(root))
    with locks.file_lock(Path(root) / ".gatewright" / "deployments" / "other.lock"):
        for action in (lambda: d.promote(r.run_id, "other"), lambda: d.rollback("other")):
            with pytest.raises(DeployError, match="in progress"):
                action()
    assert d.promote(r.run_id, "other").status == "succeeded"
    with pytest.raises(DeployError, match="Unknown environment"):
        d.promote(r.run_id, "nope")


# ---------------------------------------------------------------- L5: pages
class Paged:
    def __init__(self, pages, link=False):
        self.pages, self.link, self.asked = pages, link, []

    def request(self, method, url, etag=None, params=None):
        n = int((params or {}).get("page", 1))
        self.asked.append(n)
        if etag:
            return 304, None, {}
        data = self.pages[n - 1] if n <= len(self.pages) else []
        head = {"ETag": f"e{n}"}
        if self.link and n < len(self.pages):
            head["Link"] = '<https://evil.example/?page=2>; rel="next"'
        return 200, data, head


def test_every_page_is_read():
    http, state = Paged([[1, 2], [3, 4], [5]]), {}
    assert cached_get(http, state, "u", {"per_page": 2}) == [1, 2, 3, 4, 5]
    assert cached_get(http, state, "u", {"per_page": 2}) == [1, 2, 3, 4, 5]       # from the ETag cache
    assert http.asked == [1, 2, 3, 1, 2, 3]
    http, state = Paged([[1], [2]], link=True), {}                                # the host's own page size is smaller
    assert cached_get(http, state, "u", {"limit": 50}) == [1, 2] == cached_get(http, state, "u", {"limit": 50})
    http = Paged([[1]] * (MAX_PAGES + 5), link=True)
    with pytest.raises(VCSError, match="pages"):
        cached_get(http, {}, "u", {"limit": 50})


# ---------------------------------------------------------------- L6: closed pull requests
def test_pull_requests_off_leaves_the_queue_alone(repo, secrets):  # noqa: F811
    r = repo(extra="  pull_requests: false")
    w = r.watcher()
    item, _ = w.queue.add(VCSEvent("pull_request", "c" * 40, "x", "bob", 9), False, False)
    w.poll_once()
    assert w.queue.get(item.id).state == "pending"


def test_closed_pull_requests_leave_the_queue(repo, secrets):  # noqa: F811
    r = repo()
    r.commit("x", {"x.txt": "1"}, branch="feat")
    r.push("feat")
    r.host.add_pr(FakePR(3, "feat", "bob"))
    logs = []
    w = r.watcher(logs)
    w.poll_once()
    q = Queue(str(r.home))
    item = next(i for i in q.list() if i.pr == 3)
    late, _ = q.add(VCSEvent("pull_request", "c" * 40, "late", "bob", 9), False, False)
    assert q.close_missing(set(), late.id - 1)[0].id == item.id      # newer than the list: kept
    assert q.get(late.id).state == "pending"
    r.host.prs.clear()
    w.poll_once()
    assert q.get(late.id).state == "superseded" and "closed or merged" in q.get(late.id).note
    assert any("removed from the queue" in ln for ln in logs)
    assert next(i for i in q.list() if i.kind == "push").state == "pending"


# ---------------------------------------------------------------- L7: parallel runs
def test_parallel_runs(repo, secrets):  # noqa: F811
    r = repo(extra="  parallel: 2\n  branches: ['*']")
    # each run waits for the other: only possible when both run at the same time
    both = ("import os, pathlib, sys, time\nd = pathlib.Path(os.environ['MEET'])\n(d / str(os.getpid())).touch()\n"
            "end = time.time() + 30\nwhile len(list(d.iterdir())) < 2 and time.time() < end:\n    time.sleep(0.1)\n"
            "sys.exit(0 if len(list(d.iterdir())) >= 2 else 1)\n")
    meet = r.tmp / "meet"
    meet.mkdir()
    os.environ["MEET"] = str(meet)
    try:
        for b in ("one", "two"):
            r.commit(b, {"check.py": both, f"{b}.txt": b}, branch=b)
            r.push(b)
        w = r.watcher()
        w.poll_once()
        q = Queue(str(r.home))
        for i in q.list():
            if i.ref in ("one", "two"):
                q.approve(i.id, "me")
        runs = w.run_approved()
    finally:
        del os.environ["MEET"]
    assert sorted(x.status for x in runs) == ["passed", "passed"]
    assert len({x.run_id for x in runs}) == 2


def test_run_limit(repo, secrets):  # noqa: F811
    r = repo(mode="auto", extra="  branches: ['*']")
    r.commit("b", {"b.txt": "1"}, branch="b")
    r.push("b")
    w = r.watcher()
    w.poll_once()
    assert len(w.run_approved(limit=1)) == 1 and len(w.run_approved()) == 1


# ---------------------------------------------------------------- L8: history, submodules, LFS
def test_full_history_and_submodules(repo, secrets, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    r = repo(extra="  fetch_depth: 0\n  submodules: true")
    lib = tmp_path / "lib"
    lib.mkdir()
    git(lib, "init", "-q", "-b", "main")
    (lib / "lib.txt").write_text("from the submodule")
    git(lib, "add", "-A")
    git(lib, "commit", "-qm", "lib")
    for n in range(3):
        r.commit(f"c{n}")
    git(r.work, "-c", "protocol.file.allow=always", "submodule", "add", "-q", lib.as_uri(), "vendor")
    sha = r.commit("with submodule")
    r.push()
    w = r.watcher()
    items = w.poll_once()
    item = next(i for i in items if i.sha == sha)
    ws = w.checkout(item)
    assert (ws / "vendor" / "lib.txt").read_text(encoding="utf-8") == "from the submodule"
    assert git(ws, "rev-list", "--count", "HEAD") == "5"
    item.trusted = False                             # untrusted: no contact with servers it names
    ws = w.checkout(item)
    assert not (ws / "vendor" / "lib.txt").exists()


def test_lfs_is_pulled(repo, secrets, monkeypatch):  # noqa: F811
    r = repo(extra="  lfs: true")
    w = r.watcher()
    item = w.poll_once()[0]
    from gatewright import watch
    real, calls = subprocess.run, []

    def fake(argv, **kw):
        if "lfs" in argv:
            calls.append(argv[argv.index("lfs") + 1:])
            return subprocess.CompletedProcess(argv, 0, "", "")
        return real(argv, **kw)
    monkeypatch.setattr(watch.subprocess, "run", fake)
    w.checkout(item)
    assert calls == [["install", "--local"], ["pull", "origin"]]


def test_token_goes_to_its_own_host_only(repo, secrets):  # noqa: F811
    p = repo("gitea").watcher().provider
    p.cfg.clone_url = "https://me@git.example:3000/acme/app.git"
    env = p.git_auth_env()
    assert env["GIT_CONFIG_KEY_0"] == "http.https://git.example:3000/.extraHeader"
    p.cfg.clone_url = "ssh://git@git.example/acme/app.git"
    assert p.git_auth_env() == {}


# ---------------------------------------------------------------- L9: old runs
def test_clean(project, capsys, monkeypatch):
    root = project(BASIC_PIPELINE + "environments:\n  prod: {deploy: 'echo ok'}\n", SIMPLE)
    eng = Engine(load_pipeline(root))
    ids = [eng.run(Selection(reports=["md"])).run_id for _ in range(4)]
    assert cleanup.deployed_runs(str(root)) == set()
    Deployer(load_pipeline(root)).promote(ids[0], "prod")
    runs = Path(root) / ".gatewright" / "runs"
    (runs / "20200101-000000-dead").mkdir()           # not in the history: maybe still running
    assert cleanup.old_runs(str(root), 1) == [ids[2], ids[1]]
    assert cleanup.old_runs(str(root), 0) == [ids[2], ids[1]]    # the newest run always stays
    assert cleanup.old_runs(str(root), 1, days=1) == []
    assert cli.main(["-C", str(root), "clean", "--keep", "1", "--dry-run"]) == 0
    assert "would delete" in capsys.readouterr().out and (runs / ids[1]).exists()
    with monkeypatch.context() as m:
        def busy(*a, **k):
            raise PermissionError("in use")
        m.setattr(cleanup.shutil, "rmtree", busy)
        assert cli.main(["-C", str(root), "clean", "--keep", "1"]) == 1
    assert cli.main(["-C", str(root), "clean", "--keep", "1"]) == 0
    assert "2 run(s)" in capsys.readouterr().out
    left = sorted(p.name for p in runs.iterdir())
    assert left == sorted([ids[0], ids[3], "20200101-000000-dead"] +
                          [f"gatewright-run-{i}.tar.gz" for i in (ids[0], ids[3])])
    assert [r["id"] for r in eng.history.list(10)] == ids[::-1]   # the history is kept


def test_keep_runs(project):
    root = project(BASIC_PIPELINE + "keep_runs: 2\n", SIMPLE)
    ids = [Engine(load_pipeline(root)).run(Selection(reports=["md"])).run_id for _ in range(3)]
    assert sorted(p.name for p in (Path(root) / ".gatewright" / "runs").iterdir() if p.is_dir()) == sorted(ids[1:])


# ---------------------------------------------------------------- L11: identical lines
def test_identical_lines_are_separate_findings(tmp_path):
    (tmp_path / "a.py").write_text("eval(x)\ny = 1\neval(x)\n")

    def err(line):
        return JobError("j", "finding", "high", "eval", file="a.py", line=line, rule="E1", tool="t")
    one = finalize_all([err(1)], str(tmp_path))[0].fingerprint
    errs = finalize_all([err(3), err(1), err(1)], str(tmp_path))
    assert errs[1].fingerprint == errs[2].fingerprint == one      # existing baselines still match the first
    assert errs[0].fingerprint != one
    again = [e.fingerprint for e in finalize_all(errs, str(tmp_path))]
    assert again == [e.fingerprint for e in errs]                 # stable when repeated


def test_a_baseline_covers_one_of_two_identical_lines(project):
    """Regression: accepting one finding also hid every identical line added later."""
    show = ("import json\nout = [dict(code='S307', message='eval', filename='a.py', location=dict(row=n, column=1))\n"
            "       for n, line in enumerate(open('a.py'), 1) if 'eval' in line]\nprint(json.dumps(out))\n"
            "raise SystemExit(1)\n")
    root = project(BASIC_PIPELINE, f"""\
        jobs:
          - {{id: a, gate: commit, cache: false, run: '{PY} show.py', parser: ruff}}
    """, files={"a.py": "eval(x)\n", "show.py": show})

    def job():
        return Engine(load_pipeline(root)).run(Selection(reports=["md"]))
    first = job()
    assert len(first.all_jobs()[0].active_errors) == 1
    Baseline.write_from_run(str(root), first, 30, "accepted")
    assert job().status == "passed"
    (root / "a.py").write_text("y = 1\neval(x)\neval(x)\n")       # the accepted line moved, and a copy was added
    again = job().all_jobs()[0]
    assert [e.line for e in again.active_errors] == [3] and len(again.errors) == 2


# ---------------------------------------------------------------- L15: notifications
def http_error(code):
    return urllib.error.HTTPError("u", code, "x", {}, None)


@pytest.mark.parametrize("e, again", [
    (http_error(503), True), (http_error(429), True), (http_error(401), False),
    (smtplib.SMTPResponseException(421, "later"), True), (smtplib.SMTPResponseException(535, "auth"), False),
    (urllib.error.URLError("refused"), True), (urllib.error.URLError(ssl.SSLCertVerificationError("bad")), False),
    (ssl.SSLCertVerificationError("bad"), False), (TimeoutError(), True), (ValueError("x"), False)])
def test_what_is_retried(e, again):
    assert notify.is_temporary(e) is again


def test_temporary_failures_are_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(notify, "RETRY_WAITS", [0, 0])
    tries = []

    def post(url, body, headers, method="POST"):
        tries.append((url, headers))
        if len(tries) < 3:
            raise http_error(503)
    monkeypatch.setattr(notify.Notifier, "_post", staticmethod(post))
    monkeypatch.setenv("GATEWRIGHT_SECRET_MX", "t")
    cfg = {"channels": [{"type": "matrix", "homeserver": "https://m", "room": "!r", "token_secret": "MX",
                         "on": ["deployed"]}]}
    n = notify.Notifier(cfg, SecretStore(str(tmp_path)))
    n.deployment("deployed", "prod", "r", "d")
    assert n.warnings == [] and len(tries) == 3
    assert len({u for u, _ in tries}) == 1          # the same Matrix transaction: no duplicate messages


def test_ntfy_title_keeps_its_characters(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(notify.Notifier, "_post", staticmethod(lambda u, b, h, method="POST": seen.append(h["Title"])))
    n = notify.Notifier({"channels": [{"type": "ntfy", "url": "https://n/x", "on": ["e"]}]}, SecretStore(str(tmp_path)))
    n.send("e", "Blåbær ✓", "t")
    n.send("e", "plain", "t")
    assert base64.b64decode(seen[0][len("=?UTF-8?B?"):-2]).decode() == "Blåbær ✓" and seen[1] == "plain"


# ---------------------------------------------------------------- L18: the same 100% on any Linux (WSL, GPU, busy)
def test_progress_shows_waiting_jobs(capsys):
    cli._Progress(True)("job_waiting", {"job_id": "a", "reason": "CPU busy"})
    assert "CPU busy" in capsys.readouterr().out


@pytest.mark.parametrize("wsl", [True, False])
def test_doctor_names_wsl(monkeypatch, wsl):
    info = platform_info.detect()
    info.wsl = wsl
    monkeypatch.setattr(platform_info, "detect", lambda: info)
    detail = next(c.detail for c in doctor.run_doctor(None) if c.name == "os")
    assert detail.endswith("WSL") is wsl


def test_machine_without_a_gpu_tool(monkeypatch):
    monkeypatch.setattr(hardware.shutil, "which", lambda n: None)
    assert hardware._gpus() == []


def test_shims_are_made_once_by_parallel_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "gatewright_tools_dir", lambda: str(tmp_path))
    monkeypatch.setattr(runner, "_SHIMS", None)
    got = []
    threads = [threading.Thread(target=lambda: got.append(runner._python_shims())) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(got)) == 1


# ---------------------------------------------------------------- L16: offline install
@pytest.mark.skipif(not shutil.which("sh"), reason="needs sh")
def test_install_sh_offline_never_contacts_the_network(tmp_path):
    (tmp_path / "wheel folder").mkdir()
    sh = Path(__file__).resolve().parents[1] / "install.sh"
    if os.name == "nt":
        pytest.skip("install.sh is for Linux and macOS")
    p = subprocess.run(["sh", str(sh), "--dry-run", "--offline", str(tmp_path / "wheel folder"), "--prefix",
                        str(tmp_path / "p"), "--bin-dir", str(tmp_path / "b")], capture_output=True, text=True)
    out = p.stdout + p.stderr
    assert p.returncode == 0, out
    assert "Skipping system packages" in out and "--upgrade pip" not in out
    p = subprocess.run(["sh", str(sh), "--dry-run", "--offline", str(tmp_path / "gone")], capture_output=True, text=True)
    assert p.returncode != 0 and "not a folder" in p.stderr


# ---------------------------------------------------------------- found with the real tools
def test_secret_scan_ignores_gatewrights_own_reports(tmp_path):
    """Regression: after removing a leaked key the scan kept failing on the old run's report."""
    from gatewright.errors import parse_output
    own = {"RuleID": "r", "Description": "d", "File": ".gatewright/runs/1/gitleaks.json", "StartLine": 1}
    real = {**own, "File": "conf.py"}
    job = Job(id="s", gate="g", run="gitleaks", parser={"type": "gitleaks"})
    errs, m, _ = parse_output(job, "", "", 1, str(tmp_path), json.dumps([own, real]))
    assert [e.file for e in errs] == ["conf.py"] and m == {"findings.ignored": 1}
    assert parse_output(job, "", "", 1, str(tmp_path), json.dumps([real]))[1] == {}


def test_secret_scan_passes_again_after_the_key_is_removed(project):
    scan = ("import json, pathlib, sys@files = [p for p in pathlib.Path('.').rglob('*') if p.is_file()"
            " and p.suffix in ('.py', '.json', '.md') and 'TOPSECRET99' in p.read_text(errors='replace')"
            " and p.name != 'scan.py']@"
            "print(json.dumps([dict(RuleID='r', Description='d', File=p.as_posix(), StartLine=1) for p in files]))@"
            "sys.exit(1 if files else 0)@").replace("@", "\n")
    root = project(BASIC_PIPELINE, f"""\
        jobs:
          - {{id: s, gate: commit, cache: false, run: '{PY} scan.py', parser: gitleaks}}
    """, files={"scan.py": scan, "conf.py": "key = 'TOPSECRET99'\n"})

    def run():
        return Engine(load_pipeline(root)).run(Selection(reports=["md", "html"]))
    first = run()
    err = first.all_jobs()[0].errors[0]
    assert first.status == "failed" and err.snippet == [(1, "(not shown: this line contains a secret)")]
    for f in Path(first.artifact_dir).rglob("*"):
        assert not f.is_file() or b"TOPSECRET99" not in f.read_bytes(), f     # not copied into the artifact
    (root / "conf.py").write_text("key = None\n")
    # a tool that doesn't redact left the secret in an old run's files
    (Path(first.artifact_dir) / "old.json").write_text("TOPSECRET99")
    assert run().status == "passed"


def test_gitlab_status_that_is_already_set(monkeypatch):
    from gatewright.vcs import VCSConfig
    from gatewright.vcs.gitlab import GitLabProvider
    p = GitLabProvider(VCSConfig("gitlab", "a/b"), "t")

    def refuse(*a, **k):
        raise VCSError('HTTP 400 {"message":"Cannot transition status via :enqueue from :pending (Reason(s): ...)"}')
    monkeypatch.setattr(p.http, "request", refuse)
    p.set_status("a" * 40, "gatewright/commit", "pending", "waiting")      # regression: logged as an error
    with pytest.raises(VCSError):
        p.set_status("a" * 40, "gatewright/commit", "success", "ok")


@pytest.mark.parametrize("podman, info, engine, want", [
    ("true", "", "podman", ["--userns=keep-id", "--user"]),
    ("false", "", "docker", ["--user"]),                             # podman as root
    ("", "[name=seccomp name=rootless]", "rootless", []),
    ("", "[name=seccomp]", "docker", ["--user"]),
    (None, "", "docker", ["--user"])])
def test_container_engines(monkeypatch, tmp_path, podman, info, engine, want):
    runner._ttl_cache.clear()

    def fake(argv, **kw):
        if podman is None:
            raise OSError("no docker")
        return subprocess.CompletedProcess(argv, 0, podman if "Rootless" in argv[-1] else info, "")
    monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(os, "getgid", lambda: 1000, raising=False)
    monkeypatch.setattr(runner.subprocess, "run", fake)
    assert runner.container_engine() == engine
    argv, _ = runner._docker_argv(Job(id="x", gate="g", run="x", image="i"), str(tmp_path), "r", str(tmp_path / "o"), [])
    assert [a for a in argv if a.startswith("--user")] == want
    runner._ttl_cache.clear()


# ---------------------------------------------------------------- big repositories and rate limits
def test_named_branches_are_asked_for_one_by_one(repo, secrets):  # noqa: F811
    """Regression: every page of every branch was read to watch `main` (62 requests for one big repository)."""
    r = repo()
    w = r.watcher()
    w.poll_once()
    paths = [p for m, p, _ in r.host.requests if m == "GET" and "/branches" in p]
    assert paths == ["/repos/acme/app/branches/main", "/repos/acme/app/branches/master"]
    del r.host.requests[:]
    w.poll_once()                                    # `master` doesn't exist: not asked for again at once
    assert [p for m, p, _ in r.host.requests if "/branches" in p] == ["/repos/acme/app/branches/main"]
    git(r.bare, "branch", "master", "main")
    state = w.queue.get_state("poll_state")
    w.queue.set_state("poll_state", {k: (0 if k.startswith("missing:") else v) for k, v in state.items()})
    assert [i.ref for i in w.poll_once()] == ["master"]          # found when it appears later


def test_patterns_and_unknown_branches_use_the_list(repo, secrets):  # noqa: F811
    for branches in ("['ma*']", "[nope]"):
        r = repo(extra=f"  branches: {branches}")
        items = r.watcher().poll_once()
        assert any(p.endswith("/branches") for _, p, _ in r.host.requests)
        assert [i.ref for i in items] == (["main"] if "*" in branches else [])
        break
    w = repo_with(r, "[nope]")
    assert w.poll_once() == []


def repo_with(r, branches):
    cfg = r.home / ".gatewright" / "pipeline.yml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("branches: ['ma*']", f"branches: {branches}"), encoding="utf-8")
    return r.watcher()


def test_branch_lookup_errors_are_reported(repo, secrets):  # noqa: F811
    r = repo()
    logs = []
    w = r.watcher(logs)
    r.host.fail_with = 500
    assert w.poll_once() == [] and "HTTP 500" in logs[-1] and w.paused_until == 0


def test_poll_state_keeps_only_what_is_needed(repo, secrets):  # noqa: F811
    """Regression: the full text of every open pull request was stored on every poll."""
    r = repo()
    r.commit("x", {"x.txt": "1"}, branch="feat")
    r.push("feat")
    r.host.add_pr(FakePR(3, "feat", "bob"))
    handle = r.host.handle

    def chatty(method, path, data):
        code, obj = handle(method, path, data) or (404, None)
        if path.endswith("/pulls"):
            obj = [{**o, "body": "B" * 100_000, "labels": [1]} for o in obj]
        return code, obj
    r.host.handle = chatty
    w = r.watcher()
    items = w.poll_once()
    assert any(i.pr == 3 and i.author == "bob" for i in items)
    assert len(json.dumps(w.queue.get_state("poll_state"))) < 5000
    assert w.poll_once() == []                                   # the shortened copy is enough


def test_slim():
    assert slim({"a": 1, "b": {"c": 2, "d": 3}, "e": None}, {"a": 0, "b": {"c": 0}, "e": {"x": 0}, "z": 0}) == \
        {"a": 1, "b": {"c": 2}, "e": None}
    assert slim([1], None) == [1]


@pytest.mark.parametrize("code, headers, want", [
    (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "5000"}, 5000), (429, {"Retry-After": "30"}, 1030),
    (403, {"retry-after": "5"}, 1005), (429, {}, 1060), (429, {"RateLimit-Remaining": "0", "RateLimit-Reset": "7000"}, 7000),
    (403, {"X-RateLimit-Remaining": "12", "X-RateLimit-Reset": "5000"}, 0), (403, {}, 0), (404, {"Retry-After": "9"}, 0),
    (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "soon"}, 0)])
def test_rate_limit_end(code, headers, want):
    assert rate_limit_end(code, headers, 1000.0) == want


def test_a_rate_limited_host_is_left_alone(repo, secrets, monkeypatch):  # noqa: F811
    r = repo()
    logs = []
    w = r.watcher(logs)

    def limited(*a, **k):
        raise urllib.error.HTTPError("u", 403, "x", {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(time.time()) + 3600)}, None)
    with monkeypatch.context() as m:
        m.setattr(vcs_base.urllib.request, "urlopen", limited)
        with pytest.raises(VCSError, match="limits how often"):
            Http({}, "GitHub").request("GET", "https://api.github.com/x")
        assert w.poll_once() == []
    assert "Asking again at" in logs[-1] and w.paused_until > time.time() + 3000
    polls = []
    monkeypatch.setattr(w, "poll_once", lambda: polls.append(1))
    t = threading.Thread(target=w.loop)
    t.start()
    time.sleep(1)
    w.stop.set()
    t.join(timeout=10)
    assert polls == [1]


def test_webhooks_are_answered_before_the_statuses_are_posted(repo, secrets, monkeypatch):  # noqa: F811
    """Regression: the answer came after several calls to the host; GitHub gives up after 10 seconds."""
    import hashlib
    import hmac
    import urllib.request
    monkeypatch.setenv("GATEWRIGHT_SECRET_VCS_WEBHOOK_SECRET", "s")
    r = repo()
    logs = []
    w = r.watcher(logs)
    w.cfg.webhook = {"port": 0, "secret": "VCS_WEBHOOK_SECRET"}
    slow = threading.Event()
    real = w.provider.set_status

    def set_status(*a):
        slow.wait(20)
        if a[0] == "e" * 40:
            raise RuntimeError("broken host")
        real(*a)
    monkeypatch.setattr(w.provider, "set_status", set_status)
    host, port = w.start_webhook()

    def push(sha):
        body = json.dumps({"ref": "refs/heads/main", "after": sha}).encode()
        sig = "sha256=" + hmac.new(b"s", body, hashlib.sha256).hexdigest()
        req = urllib.request.Request(f"http://{host}:{port}/hook", body, {"X-GitHub-Event": "push", "X-Hub-Signature-256": sig})
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 202 and resp.headers["Content-Length"] == str(len(resp.read()))
    try:
        t = time.monotonic()
        push("e" * 40)
        push(r.sha)
        assert time.monotonic() - t < 5 and r.host.statuses == []
        q = Queue(str(r.home))
        item = next(i for i in q.list() if i.sha == r.sha)       # stored before it was answered
        q.approve(item.id, "me")
        threading.Timer(1, slow.set).start()
        assert len(w.run_approved()) == 1
        mine = [x["state"] for x in r.host.statuses if x["sha"] == r.sha and x["context"] == "gatewright/commit"]
        assert mine[0] == "pending" and mine[-1] == "success"    # "waiting" came first, not last
        assert any("broken host" in ln for ln in logs)           # and one failure didn't stop the others
    finally:
        slow.set()
        w._server.shutdown()
