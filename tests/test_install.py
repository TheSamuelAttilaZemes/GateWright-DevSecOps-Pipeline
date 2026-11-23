# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""install.sh: distribution detection and package-manager commands for many
Linux distributions (dry run, driven by real os-release files), plus a check
that the shell script and gatewright.platform_info agree."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from gatewright import platform_info

# install.sh is the Linux/macOS installer; Windows uses pip directly.
pytestmark = pytest.mark.skipif(os.name == "nt", reason="install.sh is for Linux/macOS")

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
SH = shutil.which("sh")
FIXTURES = Path(__file__).parent / "fixtures" / "os-release"

# fixture -> (family, package manager, a command that must appear, a package that must appear)
EXPECTED = {
    "kali": ("debian", "apt", "apt-get install -y --no-install-recommends", "python3-venv"),
    "debian": ("debian", "apt", "apt-get install", "python3-venv"),
    "ubuntu": ("debian", "apt", "apt-get install", "python3-venv"),
    "linuxmint": ("debian", "apt", "apt-get install", "python3-venv"),
    "parrot": ("debian", "apt", "apt-get install", "python3-venv"),
    "pop": ("debian", "apt", "apt-get install", "python3-venv"),
    "fedora": ("rhel", "dnf", "dnf install -y", "python3-pip"),
    "rhel": ("rhel", "dnf", "dnf install -y", "python3-pip"),
    "rocky": ("rhel", "dnf", "dnf install -y", "python3-pip"),
    "almalinux": ("rhel", "dnf", "dnf install -y", "python3-pip"),
    "amzn": ("rhel", "dnf", "dnf install -y", "python3-pip"),
    "arch": ("arch", "pacman", "pacman -S --needed --noconfirm", "python-pip"),
    "manjaro": ("arch", "pacman", "pacman -S --needed --noconfirm", "python-pip"),
    "endeavouros": ("arch", "pacman", "pacman -S --needed --noconfirm", "python-pip"),
    "blackarch": ("arch", "pacman", "pacman -S --needed --noconfirm", "python-pip"),
    "opensuse-tumbleweed": ("suse", "zypper", "zypper --non-interactive install", "python3-pip"),
    "opensuse-leap": ("suse", "zypper", "zypper --non-interactive install", "python3-pip"),
    "sles": ("suse", "zypper", "zypper --non-interactive install", "python3-pip"),
    "alpine": ("alpine", "apk", "apk add --no-cache", "py3-pip"),
    "void": ("void", "xbps", "xbps-install -Sy", "python3-pip"),
    "gentoo": ("gentoo", "emerge", "emerge --noreplace", "dev-lang/python"),
    "nixos": ("nixos", "nix", "nix-shell -p", "python3"),
    "unknown-like-ubuntu": ("debian", "apt", "apt-get install", "python3-venv"),
}


def dry_run(fixture: str, tmp_path: Path, extra=(), env=None) -> str:
    out = subprocess.run(
        [SH, str(INSTALL), "--dry-run", "--yes", "--os-release", str(FIXTURES / fixture),
         "--prefix", str(tmp_path / "prefix"), "--bin-dir", str(tmp_path / "bin"), *extra],
        capture_output=True, text=True, env={**os.environ, "NO_COLOR": "1", **(env or {})}, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    return out.stdout + out.stderr


@pytest.mark.parametrize("fixture", sorted(EXPECTED))
def test_installer_picks_right_package_manager(fixture, tmp_path):
    family, pm, cmd, pkg = EXPECTED[fixture]
    out = dry_run(fixture, tmp_path)
    assert f"Family:    {family}   package manager: {pm}" in out, out
    assert cmd in out, out
    assert pkg in out, out
    assert not (tmp_path / "prefix").exists(), "dry run must not change anything"
    if family != "nixos":
        assert "-m venv" in out and "pip install" in out


def test_kali_details(tmp_path):
    out = dry_run("kali", tmp_path, extra=["--with-docker"])
    assert "Kali GNU/Linux Rolling" in out
    assert "apt-get update" in out
    assert "libpango-1.0-0" in out and "docker.io" in out


def test_no_pdf_and_no_system(tmp_path):
    out = dry_run("fedora", tmp_path, extra=["--no-pdf"])
    assert "pango" not in out
    out = dry_run("arch", tmp_path, extra=["--no-system"])
    assert "pacman" not in out.split("Family:")[1].split("\n", 1)[1]
    assert "Skipping system packages" in out


def test_unknown_distro_without_package_manager(tmp_path):
    # PATH with only the basic tools the script needs: no apt/dnf/pacman to guess from
    bindir = tmp_path / "minibin"
    bindir.mkdir()
    for tool in ("sed", "head", "tr", "uname", "id", "dirname", "grep", "date", "env", "mkdir", "ln", "rm", "cat"):
        src = shutil.which(tool)
        if src:
            os.symlink(src, bindir / tool)
    py = shutil.which("python3")
    os.symlink(py, bindir / "python3")
    out = dry_run("unknown", tmp_path, env={"PATH": str(bindir)})
    assert "Family:    unknown" in out
    assert "Unknown distribution - skipping system packages" in out


def test_python_detection_matches_shell(tmp_path, monkeypatch):
    """gatewright.platform_info and install.sh must classify every distro the same way."""
    monkeypatch.setattr(platform_info.platform, "system", lambda: "Linux")      # also when tested on macOS
    for fixture, (family, pm, _, _) in EXPECTED.items():
        info = platform_info.detect(str(FIXTURES / fixture), which=lambda n: None)
        assert info.family == family, fixture
        assert info.package_manager == pm, fixture


def test_family_tables_in_sync():
    """Every distro ID listed in install.sh's family_of() is known to platform_info and vice versa."""
    text = INSTALL.read_text(encoding="utf-8")
    body = text[text.index("family_of() {"):text.index("detect_os() {")]
    shell_ids = {}
    for line in body.splitlines():
        m = re.match(r"\s*([a-z0-9|.\-*]+)\)\s+echo (\w+)", line)
        if m:
            for i in m.group(1).split("|"):
                if "*" not in i and i != "redhat":
                    shell_ids[i] = m.group(2)
    missing_in_py = set(shell_ids) - set(platform_info.FAMILY_BY_ID)
    missing_in_sh = {k for k, v in platform_info.FAMILY_BY_ID.items()
                     if v in {"debian", "rhel", "arch", "suse", "alpine", "void", "gentoo", "nixos"}} - set(shell_ids)
    assert not missing_in_py, missing_in_py
    assert not missing_in_sh, missing_in_sh
    for k, fam in shell_ids.items():
        assert platform_info.FAMILY_BY_ID[k] == fam, k


def test_doctor_hint_for_kali_uses_apt(monkeypatch):
    monkeypatch.setattr(platform_info.platform, "system", lambda: "Linux")
    info = platform_info.detect(str(FIXTURES / "kali"), which=lambda n: None)
    assert platform_info.install_hint("gitleaks", info) == "sudo apt install -y gitleaks"
    assert platform_info.install_hint("trivy", info) == "sudo apt install -y trivy"
    arch = platform_info.detect(str(FIXTURES / "arch"), which=lambda n: None)
    assert platform_info.install_hint("gitleaks", arch) == "sudo pacman -S --needed gitleaks"
    fed = platform_info.detect(str(FIXTURES / "fedora"), which=lambda n: None)
    assert platform_info.install_hint("gitleaks", fed, "see website") == "see website"
    assert platform_info.install_hint("docker", fed) == "sudo dnf install -y moby-engine"


@pytest.mark.parametrize("shell", ["sh", "dash", "bash", "busybox ash"])
def test_installer_runs_under_common_shells(shell, tmp_path):
    exe = shell.split()[0]
    if not shutil.which(exe):
        pytest.skip(f"{exe} not installed")
    out = subprocess.run([*shell.split(), str(INSTALL), "--dry-run", "--yes", "--os-release",
                          str(FIXTURES / "kali"), "--prefix", str(tmp_path / "p"), "--bin-dir", str(tmp_path / "b")],
                         capture_output=True, text=True, env={**os.environ, "NO_COLOR": "1"}, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "package manager: apt" in out.stdout


def test_shellcheck_clean():
    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck not installed")
    out = subprocess.run(["shellcheck", "-s", "sh", str(INSTALL)], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout
