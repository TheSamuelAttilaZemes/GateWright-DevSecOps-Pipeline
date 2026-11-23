# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Secrets: injected at runtime, masked everywhere, never written to artifacts.

Lookup order for a secret named X:
  1. environment variable GATEWRIGHT_SECRET_X
  2. environment variable X
  3. .gatewright/secrets.env  (KEY=value lines; must not be world-readable on posix)
"""

from __future__ import annotations

import base64
import json
import os
import stat
import sys
import urllib.parse
from pathlib import Path

from .winsupport import file_sddl, secrets_file_warning

MASK = "***"


def _base64_forms(value: bytes) -> set[str]:
    """How `value` looks inside base64 text, whatever comes before and after it
    (e.g. `user:<secret>` in a Basic header): one form for each of the three alignments."""
    out = {base64.b64encode(value).decode()}
    for offset, skip in ((0, 0), (1, 2), (2, 3)):     # skip the characters that mix in foreign bits
        text = base64.b64encode(b"\0" * offset + value).decode()
        out.add(text[skip:(offset + len(value)) * 8 // 6])
    return out | {t.replace("+", "-").replace("/", "_") for t in out}


def encoded_forms(value: str) -> set[str]:
    """`value` itself and the encodings it is likely to be printed in."""
    out = {value}
    lines = [ln.strip() for ln in value.splitlines()]
    if len(lines) > 1:             # e.g. a private key: output is masked line by line
        out |= {ln for ln in lines if len(ln) >= 8 and not ln.startswith("-----")}
    if len(value) >= 8:            # shorter values would hide ordinary text
        raw = value.encode()
        forms = _base64_forms(raw) | {raw.hex(), raw.hex().upper(), urllib.parse.quote(value, safe=""),
                                      urllib.parse.quote_plus(value), json.dumps(value)[1:-1],
                                      json.dumps(value, ensure_ascii=False)[1:-1]}
        out |= {f for f in forms if len(f) >= 8}
    return out


class SecretStore:
    """Resolves secrets for jobs and masks every value that was handed out."""
    def __init__(self, root: str, enabled: bool = True):
        self.root = Path(root)
        self.file = self.root / ".gatewright" / "secrets.env"
        self.warnings: list[str] = []
        self.enabled = enabled  # False for untrusted runs: every secret is reported as withheld
        self._file_values = self._read_file()
        self._used: set[str] = set()   # every value ever handed out, so all of them stay masked
        self._hide: list[str] = []     # those values and their encodings, longest first

    def _read_file(self) -> dict[str, str]:
        if not self.file.exists():
            return {}
        if sys.platform == "win32":  # pragma: no cover - Windows only: an access list instead of mode bits
            warning = secrets_file_warning(self.file, file_sddl(self.file), os.environ.get("USERNAME", "YOU"))
            if warning:
                self.warnings.append(warning)
        else:
            mode = self.file.stat().st_mode
            if mode & (stat.S_IRGRP | stat.S_IROTH):
                self.warnings.append(
                    f"{self.file} is readable by other users. Fix with: chmod 600 {self.file}")
        values = {}
        for line in self.file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k.startswith("export "):          # `export KEY=value`, as in shell-style .env files
                k = k[len("export "):].strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]                        # only strip a matching pair of quotes
            values[k] = v
        return values

    def get(self, name: str) -> str | None:
        """Value of a secret, or None. Remembers it so `mask` can hide it."""
        if not self.enabled:
            return None
        for v in (os.environ.get(f"GATEWRIGHT_SECRET_{name}"), os.environ.get(name), self._file_values.get(name)):
            if v:
                if v not in self._used:
                    self._used.add(v)
                    forms = {f for u in self._used if len(u) >= 4 for f in encoded_forms(u)}
                    self._hide = sorted(forms, key=len, reverse=True)
                return v
        return None

    def resolve(self, names: list[str]) -> tuple[dict[str, str], list[str]]:
        """Return (env, missing)."""
        env, missing = {}, []
        for n in names:
            v = self.get(n)
            if v is None:
                missing.append(n)
            else:
                env[n] = v
        return env, missing

    @property
    def active(self) -> bool:
        """True once a secret value that mask() would hide has been handed out."""
        return any(len(v) >= 4 for v in self._used)

    def mask(self, text: str) -> str:
        """Replace every resolved secret value (4+ chars), plain or encoded, in `text` with ***."""
        if not text:
            return text
        for v in self._hide:    # longest first so overlapping secrets mask fully
            text = text.replace(v, MASK)
        return text
