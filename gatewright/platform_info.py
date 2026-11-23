# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Operating system / Linux distribution detection.

Used by `gatewright doctor` to give install commands for *your* package manager
(apt on Kali/Debian/Ubuntu, dnf on Fedora/RHEL, pacman on Arch, ...).
install.sh contains the same mapping in POSIX shell; a test keeps them in sync.
"""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import asdict, dataclass

# distro ID -> family. Anything else is resolved through ID_LIKE, then by
# looking for a known package manager on PATH.
FAMILY_BY_ID = {
    **dict.fromkeys(["debian", "ubuntu", "kali", "linuxmint", "pop", "raspbian", "parrot", "elementary",
                     "zorin", "neon", "devuan", "mx", "deepin", "pureos", "tails", "kubuntu", "xubuntu"], "debian"),
    **dict.fromkeys(["fedora", "rhel", "centos", "rocky", "almalinux", "ol", "amzn", "nobara",
                     "fedora-asahi-remix", "eurolinux", "circle"], "rhel"),
    **dict.fromkeys(["arch", "manjaro", "endeavouros", "garuda", "artix", "arcolinux", "blackarch",
                     "cachyos", "steamos"], "arch"),
    **dict.fromkeys(["opensuse", "opensuse-leap", "opensuse-tumbleweed", "opensuse-microos", "sles",
                     "sled", "suse"], "suse"),
    "alpine": "alpine", "postmarketos": "alpine",
    "void": "void",
    "gentoo": "gentoo", "funtoo": "gentoo",
    "nixos": "nixos",
    "slackware": "slackware",
    "clear-linux-os": "clear",
}

PM_BY_FAMILY = {"debian": "apt", "rhel": "dnf", "arch": "pacman", "suse": "zypper", "alpine": "apk",
                "void": "xbps", "gentoo": "emerge", "nixos": "nix", "slackware": "slackpkg", "clear": "swupd"}

PM_INSTALL = {
    "apt": "sudo apt install -y {pkgs}",
    "dnf": "sudo dnf install -y {pkgs}",
    "yum": "sudo yum install -y {pkgs}",
    "pacman": "sudo pacman -S --needed {pkgs}",
    "zypper": "sudo zypper install -y {pkgs}",
    "apk": "sudo apk add {pkgs}",
    "xbps": "sudo xbps-install -Sy {pkgs}",
    "emerge": "sudo emerge --ask {pkgs}",
    "nix": "nix-shell -p {pkgs}",
    "brew": "brew install {pkgs}",
    "winget": "winget install {pkgs}",
}

# tool -> package name per family (only where the distro actually packages it)
TOOL_PACKAGES: dict[str, dict[str, str]] = {
    "git": {"debian": "git", "rhel": "git", "arch": "git", "suse": "git", "alpine": "git", "void": "git",
            "gentoo": "dev-vcs/git", "nixos": "git", "macos": "git", "windows": "Git.Git"},
    "docker": {"debian": "docker.io", "rhel": "moby-engine", "arch": "docker", "suse": "docker",
               "alpine": "docker", "void": "docker", "gentoo": "app-containers/docker", "nixos": "docker",
               "macos": "--cask docker", "windows": "Docker.DockerDesktop"},
    "gitleaks": {"kali": "gitleaks", "arch": "gitleaks", "alpine": "gitleaks", "nixos": "gitleaks",
                 "macos": "gitleaks", "windows": "Gitleaks.Gitleaks"},
    "trivy": {"kali": "trivy", "arch": "trivy", "alpine": "trivy", "nixos": "trivy", "macos": "trivy",
              "windows": "AquaSecurity.Trivy"},
    "hadolint": {"arch": "hadolint-bin", "nixos": "hadolint", "macos": "hadolint"},
    "npm": {"debian": "npm", "rhel": "npm", "arch": "npm", "suse": "npm", "alpine": "npm", "void": "nodejs",
            "gentoo": "net-libs/nodejs", "nixos": "nodejs", "macos": "node", "windows": "OpenJS.NodeJS"},
    "pango": {"debian": "libpango-1.0-0 libpangoft2-1.0-0", "rhel": "pango", "arch": "pango", "suse": "pango",
              "alpine": "pango", "void": "pango", "gentoo": "x11-libs/pango", "nixos": "pango",
              "macos": "pango"},
}

PIP_TOOLS = {"ruff", "bandit", "pytest", "mypy", "coverage", "pip-audit", "semgrep", "checkov", "yamllint"}


@dataclass
class OSInfo:
    """What OS/distribution this is and how to install software on it."""
    system: str          # Linux | Darwin | Windows
    distro_id: str       # kali, ubuntu, fedora, ... ("" outside Linux)
    id_like: list[str]
    name: str            # PRETTY_NAME
    version: str
    family: str          # debian | rhel | arch | suse | alpine | void | gentoo | nixos | macos | windows | unknown
    package_manager: str
    wsl: bool
    externally_managed: bool  # PEP 668: system pip is locked -> use a venv/pipx

    def to_dict(self) -> dict:
        return asdict(self)


def parse_os_release(text: str) -> dict[str, str]:
    """Parse /etc/os-release (KEY=value lines, optional quotes). Never executes it."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def family_for(distro_id: str, id_like: list[str], which=shutil.which) -> str:
    """Distribution family from ID, then ID_LIKE, then by which package manager exists."""
    if distro_id in FAMILY_BY_ID:
        return FAMILY_BY_ID[distro_id]
    for like in id_like:
        if like in FAMILY_BY_ID:
            return FAMILY_BY_ID[like]
        if like.startswith("rhel") or like in ("redhat", "centos"):
            return "rhel"
        if like.startswith("suse"):
            return "suse"
    for pm, fam in (("apt-get", "debian"), ("dnf", "rhel"), ("yum", "rhel"), ("pacman", "arch"),
                    ("zypper", "suse"), ("apk", "alpine"), ("xbps-install", "void"), ("emerge", "gentoo")):
        if which(pm):
            return fam
    return "unknown"


def detect(os_release_path: str | None = None, which=shutil.which) -> OSInfo:
    system = platform.system()
    if system == "Darwin":
        return OSInfo("Darwin", "", [], f"macOS {platform.mac_ver()[0]}", platform.mac_ver()[0], "macos",
                      "brew" if which("brew") else "", False, False)
    if system == "Windows":
        return OSInfo("Windows", "", [], f"Windows {platform.release()}", platform.version(), "windows",
                      "winget" if which("winget") else "", False, False)
    data: dict[str, str] = {}
    for path in ([os_release_path] if os_release_path else ["/etc/os-release", "/usr/lib/os-release"]):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:  # odd bytes must not crash detection
                data = parse_os_release(fh.read())
            break
        except OSError:
            continue
    did = data.get("ID", "").lower()
    like = data.get("ID_LIKE", "").lower().split()
    fam = family_for(did, like, which)
    pm = PM_BY_FAMILY.get(fam, "")
    if fam == "rhel" and not which("dnf") and which("yum"):
        pm = "yum"
    try:
        with open("/proc/version", encoding="utf-8") as fh:
            wsl = "microsoft" in fh.read().lower()
    except OSError:
        wsl = False
    import sysconfig
    stdlib = sysconfig.get_paths().get("stdlib", "")
    managed = bool(stdlib) and os.path.exists(os.path.join(stdlib, "EXTERNALLY-MANAGED"))
    system = system or "Linux"     # FreeBSD etc. say so (they may still ship an os-release file)
    return OSInfo(system, did, like, data.get("PRETTY_NAME", data.get("NAME", system)),
                  data.get("VERSION_ID", ""), fam, pm, wsl, managed)


def install_hint(tool: str, info: OSInfo, fallback: str = "") -> str:
    """Install command for `tool` on this OS, or the fallback text."""
    if tool in PIP_TOOLS:
        import sys
        if sys.prefix != sys.base_prefix:  # Gatewright runs in its own venv: install the tool next to it
            return f"{sys.executable} -m pip install {tool}"
        if info.externally_managed:
            return f"pipx install {tool}"
        return f"pip install {tool}"
    pkgs = TOOL_PACKAGES.get(tool, {})
    pkg = pkgs.get(info.distro_id) or pkgs.get(info.family)
    if pkg and info.package_manager in PM_INSTALL:
        return PM_INSTALL[info.package_manager].format(pkgs=pkg)
    return fallback
