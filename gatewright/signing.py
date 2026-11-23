# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Artifact signing (v0.6) with SSH signatures (`ssh-keygen -Y sign/verify`).

Why SSH signatures: OpenSSH (8.2+, for `-Y sign/verify/find-principals`) is already on almost every Linux and macOS
machine and on Windows 10+, so signing needs no extra dependency, and the same
Ed25519 keys work for git commit signing too.

What is signed: `checksums.sha256` of a run. It lists the SHA-256 of every
file in the artifact, so one signature covers the whole artifact.

    run folder/
      checksums.sha256        sha256 of every file
      checksums.sha256.sig    SSH signature over checksums.sha256 (namespace "gatewright")
      signature.json          who signed (identity), key fingerprint, when

Verification needs an *allowed signers* file (OpenSSH format, one line per
trusted key: `alice@example.com namespaces="gatewright" ssh-ed25519 AAAA...`),
by default `.gatewright/allowed_signers` committed in the repository.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # nosec B404 - fixed ssh-keygen argv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

NAMESPACE = "gatewright"
SIG_FILE = "checksums.sha256.sig"
META_FILE = "signature.json"


class SigningError(Exception):
    """Signing or verification could not be done (missing tool, key, file...)."""


def default_key_path() -> Path:
    base = os.environ.get("APPDATA") if os.name == "nt" else os.environ.get("XDG_CONFIG_HOME")
    return Path(base or Path.home() / ".config") / "gatewright" / "signing_key"


@dataclass
class SigningConfig:
    """The `signing:` section of pipeline.yml."""
    key: str = ""
    identity: str = ""
    allowed_signers: str = ".gatewright/allowed_signers"
    auto: bool = True
    required: bool = False

    @classmethod
    def from_dict(cls, d: dict | None) -> "SigningConfig":
        d = d or {}
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @property
    def key_path(self) -> Path:
        return Path(os.path.expanduser(self.key)) if self.key else default_key_path()

    def allowed_signers_path(self, root: str) -> Path:
        p = Path(os.path.expanduser(self.allowed_signers))
        return p if p.is_absolute() else Path(root) / p


def ssh_keygen() -> str:
    exe = shutil.which("ssh-keygen")
    if not exe:
        raise SigningError("ssh-keygen (OpenSSH client) is not installed. Install it: Debian/Ubuntu/Kali "
                           "`sudo apt install openssh-client`, Fedora `sudo dnf install openssh-clients`, "
                           "Alpine `apk add openssh-keygen`, Windows: Settings > Optional features > OpenSSH Client.")
    return exe


def openssh_version() -> tuple[int, int] | None:
    try:
        out = subprocess.run(["ssh", "-V"], capture_output=True, text=True, timeout=10, check=False)  # nosec B603 B607
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"OpenSSH[_for Windows]*_(\d+)\.(\d+)", out.stderr + out.stdout)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _run(args: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess:
    # never ask for a passphrase: nobody may be there to answer (the agent is asked instead, see sign())
    env = {**os.environ, "SSH_ASKPASS_REQUIRE": "force", "SSH_ASKPASS": "false"}
    try:
        return subprocess.run([ssh_keygen(), *args], input=stdin, capture_output=True, timeout=60,  # nosec B603
                              check=False, env=env)
    except subprocess.TimeoutExpired:
        raise SigningError("ssh-keygen did not answer within 60 seconds.") from None


def keygen(path: Path, identity: str, passphrase: bool = False) -> str:
    """Create an Ed25519 key pair. Without a passphrase unattended runs can sign; with one
    (ssh-keygen asks for it) they need the key in ssh-agent.
    Returns the allowed_signers line for the public key."""
    if path.exists():
        raise SigningError(f"{path} already exists; not overwriting a key.")
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["-q", "-t", "ed25519", "-C", identity, "-f", str(path)]
    if passphrase:
        p = subprocess.run([ssh_keygen(), *args], stderr=subprocess.PIPE, check=False)  # nosec B603
    else:
        p = _run([*args, "-N", ""])
    if p.returncode != 0:
        raise SigningError(f"ssh-keygen could not create the key: {p.stderr.decode().strip()}")
    if os.name == "posix":  # pragma: no branch - on Windows the key inherits the profile folder's ACL
        path.chmod(0o600)
    return allowed_signers_line(identity, path.with_name(path.name + ".pub"))


def allowed_signers_line(identity: str, pubkey_path: Path) -> str:
    parts = pubkey_path.read_text(encoding="utf-8").split()
    return f'{identity} namespaces="{NAMESPACE}" {parts[0]} {parts[1]}'


def fingerprint(pubkey_path: Path) -> str:
    p = _run(["-l", "-f", str(pubkey_path)])
    return p.stdout.decode().split()[1] if p.returncode == 0 and p.stdout else ""


def sign(run_dir: Path, cfg: SigningConfig) -> dict:
    """Sign run_dir/checksums.sha256. Returns the metadata written to signature.json."""
    checks = run_dir / "checksums.sha256"
    if not checks.exists():
        raise SigningError(f"{checks} is missing; write checksums before signing.")
    key = cfg.key_path
    pub = key if key.suffix == ".pub" else key.with_name(key.name + ".pub")
    # the private key file, else (it has a passphrase, or lives elsewhere) ssh-agent through the public key
    keys = [k for k in dict.fromkeys((key, pub)) if k.exists()]
    if not keys:
        raise SigningError(f"Signing key {key} not found. Create one with `gatewright keygen`.")
    if not cfg.identity:
        raise SigningError("signing.identity is not set (e.g. your e-mail address).")
    sig = run_dir / SIG_FILE
    if sig.exists():
        sig.unlink()
    why = []
    for k in keys:
        p = _run(["-Y", "sign", "-f", str(k), "-n", NAMESPACE, str(checks)])
        if p.returncode == 0:
            break
        why.append(p.stderr.decode(errors="replace").strip())
    else:
        raise SigningError(f"ssh-keygen could not sign: {why[0]}. If the key has a passphrase or is on a "
                           "security key, add it to ssh-agent first (`ssh-add`).")
    meta = {"identity": cfg.identity, "namespace": NAMESPACE, "fingerprint": fingerprint(pub) if pub.exists() else "",
            "signed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "signed_file": checks.name}
    (run_dir / META_FILE).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def verify(run_dir: Path, allowed_signers: Path) -> tuple[bool, str]:
    """Check the signature over checksums.sha256 against the allowed signers.
    (Checking the files against checksums.sha256 is artifacts.verify.)"""
    sig, checks, meta = run_dir / SIG_FILE, run_dir / "checksums.sha256", run_dir / META_FILE
    if not sig.exists():
        return False, "the artifact is not signed (no checksums.sha256.sig)"
    if not allowed_signers.exists():
        return False, (f"no allowed signers file at {allowed_signers}: add the public keys you trust "
                       "(see `gatewright keygen`)")
    try:
        identity = json.loads(meta.read_text(encoding="utf-8"))["identity"]
    except (OSError, ValueError, KeyError):
        return False, "signature.json is missing or unreadable, so the signer's identity is unknown"
    try:
        # 1. Which trusted signer (if any) owns the key that made this signature?
        found = _run(["-Y", "find-principals", "-s", str(sig), "-f", str(allowed_signers)])
        principals = found.stdout.decode().split() if found.returncode == 0 else []
        if not principals:
            return False, (f"the signing key is not in {allowed_signers.name}, so the signer "
                           f"(claims to be {identity}) is not trusted")
        if identity not in principals:
            return False, (f"signature.json names {identity}, but the key belongs to {', '.join(principals)}; "
                           "the metadata was edited")
        # 2. Does the signature match the checksums file?
        p = _run(["-Y", "verify", "-f", str(allowed_signers), "-I", identity, "-n", NAMESPACE, "-s", str(sig)],
                 stdin=checks.read_bytes())
    except SigningError as e:
        return False, str(e)
    if p.returncode == 0:
        return True, f"signed by {identity} ({(p.stdout or p.stderr).decode().strip().splitlines()[0][:120]})"
    err = (p.stderr or p.stdout).decode().strip()
    return False, f"signature does NOT match: the artifact or its checksums were changed after signing ({err[:160]})"
