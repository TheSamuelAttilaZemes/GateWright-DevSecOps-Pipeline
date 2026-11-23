#!/bin/sh
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Gatewright installer for Linux (any distribution) and macOS.
#
#   1. detects the OS / distribution (Kali, Debian, Ubuntu, Fedora, RHEL, Arch,
#      openSUSE, Alpine, Void, Gentoo, NixOS, macOS, ... plus WSL)
#   2. installs the system packages Gatewright needs with *that* package manager
#      (Python >= 3.10 with venv, git, and the Pango libraries for PDF reports)
#   3. installs Gatewright into its own virtual environment, so it never touches the
#      system Python (required on Kali/Debian/Ubuntu/Fedora/Arch: PEP 668)
#   4. puts a `gatewright` command in ~/.local/bin and checks that it works
#
# Usage:  sh install.sh [options]
#   -y, --yes            don't ask before installing system packages
#   -n, --dry-run        print what would be done, change nothing
#       --no-system      skip system packages (use what is already installed)
#       --no-pdf         skip the PDF libraries (Markdown/HTML reports still work)
#       --no-tools       don't install the starter scanners (ruff, bandit, pytest,
#                        coverage, pip-audit) into Gatewright's environment
#       --with-docker    also install Docker (or Podman on RHEL-likes)
#       --prefix DIR     where Gatewright lives      (default: ~/.local/share/gatewright)
#       --bin-dir DIR    where `gatewright` is linked (default: ~/.local/bin)
#       --python PATH    use this Python interpreter
#       --offline DIR    install Python packages from DIR only (no internet, see docs/tips.md)
#       --os-release F   read distribution info from F (testing)
#       --uninstall      remove Gatewright (keeps your projects' .gatewright folders)
#   -h, --help

set -eu

GATEWRIGHT_SRC=$(cd "$(dirname "$0")" && pwd)
PREFIX="${GATEWRIGHT_PREFIX:-${XDG_DATA_HOME:-$HOME/.local/share}/gatewright}"
BIN_DIR="${GATEWRIGHT_BIN_DIR:-$HOME/.local/bin}"
DRY_RUN=0
ASSUME_YES=0
DO_SYSTEM=1
WITH_PDF=1
WITH_DOCKER=0
WITH_TOOLS=1
PYTHON=""
OS_RELEASE=""
UNINSTALL=0
OFFLINE=""
BASE=""; PDF=""; PDF_FALLBACK=""; DOCKER_PKGS=""; PY_EXTRA=""

# ------------------------------------------------------------------ output
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_B=$(printf '\033[1m'); C_G=$(printf '\033[32m'); C_Y=$(printf '\033[33m')
    C_R=$(printf '\033[31m'); C_D=$(printf '\033[2m'); C_0=$(printf '\033[0m')
else
    C_B=""; C_G=""; C_Y=""; C_R=""; C_D=""; C_0=""
fi
say()  { printf '%s==>%s %s\n' "$C_B" "$C_0" "$*"; }
ok()   { printf '%s  ✓%s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '%s  !%s %s\n' "$C_Y" "$C_0" "$*" >&2; }
die()  { printf '%s  ✗ %s%s\n' "$C_R" "$*" "$C_0" >&2; exit 1; }

run() {
    printf '%s  + %s%s\n' "$C_D" "$*" "$C_0"
    [ "$DRY_RUN" -eq 1 ] && return 0
    "$@"
}

# Run a command as root: directly, or through sudo/doas.
SUDO=""
pick_sudo() {
    if [ "$(id -u)" -eq 0 ]; then SUDO=""
    elif command -v sudo >/dev/null 2>&1; then SUDO="sudo"
    elif command -v doas >/dev/null 2>&1; then SUDO="doas"
    else SUDO="NONE"
    fi
}
as_root() {
    if [ "$SUDO" = "NONE" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then run "$@"; return 0; fi
        die "Need root to run: $*  (no sudo/doas found). Run it as root, then re-run with --no-system."
    fi
    if [ -n "$SUDO" ]; then run "$SUDO" "$@"; else run "$@"; fi
}

usage() { sed -n '3,/^$/p' "$0" | sed -n 's/^# \{0,1\}//p'; exit 0; }

while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes) ASSUME_YES=1 ;;
        -n|--dry-run) DRY_RUN=1 ;;
        --no-system) DO_SYSTEM=0 ;;
        --no-pdf) WITH_PDF=0 ;;
        --with-docker) WITH_DOCKER=1 ;;
        --no-tools) WITH_TOOLS=0 ;;
        --prefix) PREFIX="$2"; shift ;;
        --bin-dir) BIN_DIR="$2"; shift ;;
        --python) PYTHON="$2"; shift ;;
        --offline) OFFLINE="$2"; DO_SYSTEM=0; shift ;;
        --os-release) OS_RELEASE="$2"; shift ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help) usage ;;
        *) die "Unknown option: $1 (see --help)" ;;
    esac
    shift
done

# ------------------------------------------------------------------ uninstall
if [ "$UNINSTALL" -eq 1 ]; then
    say "Removing Gatewright from $PREFIX and $BIN_DIR/gatewright"
    run rm -rf "$PREFIX"
    if [ -L "$BIN_DIR/gatewright" ] || [ -f "$BIN_DIR/gatewright" ]; then run rm -f "$BIN_DIR/gatewright"; fi
    ok "Gatewright removed. Project folders (.gatewright/) were left untouched."
    exit 0
fi

# ------------------------------------------------------------------ detect OS
OS_KIND=$(uname -s)
[ -n "$OS_RELEASE" ] && OS_KIND=Linux    # testing: act like that distribution, also on macOS
DISTRO_ID=""; DISTRO_LIKE=""; DISTRO_NAME=""; DISTRO_VERSION=""; FAMILY="unknown"; PM=""; WSL=0

read_os_release() {
    _f="$1"
    # Parse KEY=value lines safely (no sourcing: the file is data, not code)
    DISTRO_ID=$(sed -n 's/^ID=//p' "$_f" | head -n1 | tr -d '"'"'" | tr '[:upper:]' '[:lower:]')
    DISTRO_LIKE=$(sed -n 's/^ID_LIKE=//p' "$_f" | head -n1 | tr -d '"'"'" | tr '[:upper:]' '[:lower:]')
    DISTRO_NAME=$(sed -n 's/^PRETTY_NAME=//p' "$_f" | head -n1 | tr -d '"'"'")
    DISTRO_VERSION=$(sed -n 's/^VERSION_ID=//p' "$_f" | head -n1 | tr -d '"'"'")
}

family_of() {
    case "$1" in
        debian|ubuntu|kali|linuxmint|pop|raspbian|parrot|elementary|zorin|neon|devuan|mx|deepin|pureos|tails|kubuntu|xubuntu) echo debian ;;
        fedora|rhel|centos|rocky|almalinux|ol|amzn|nobara|fedora-asahi-remix|eurolinux|circle) echo rhel ;;
        arch|manjaro|endeavouros|garuda|artix|arcolinux|blackarch|cachyos|steamos) echo arch ;;
        opensuse|opensuse-leap|opensuse-tumbleweed|opensuse-microos|sles|sled|suse) echo suse ;;
        alpine|postmarketos) echo alpine ;;
        void) echo void ;;
        gentoo|funtoo) echo gentoo ;;
        nixos) echo nixos ;;
        rhel*|redhat) echo rhel ;;
        suse*) echo suse ;;
        *) echo unknown ;;
    esac
}

detect_os() {
    case "$OS_KIND" in
        Darwin)
            FAMILY="macos"; DISTRO_NAME="macOS $(sw_vers -productVersion 2>/dev/null || echo)"
            PM="brew"; return ;;
        Linux) ;;
        *) die "Unsupported OS: $OS_KIND. On Windows, install Python + Git for Windows, then: pip install \"${GATEWRIGHT_SRC}[pdf]\"" ;;
    esac
    _rel="$OS_RELEASE"
    if [ -z "$_rel" ]; then
        for _cand in /etc/os-release /usr/lib/os-release; do
            if [ -r "$_cand" ]; then _rel="$_cand"; break; fi
        done
    fi
    [ -n "$_rel" ] && [ -r "$_rel" ] && read_os_release "$_rel"
    FAMILY=$(family_of "$DISTRO_ID")
    if [ "$FAMILY" = "unknown" ]; then
        for _like in $DISTRO_LIKE; do
            FAMILY=$(family_of "$_like")
            [ "$FAMILY" != "unknown" ] && break
        done
    fi
    if [ "$FAMILY" = "unknown" ]; then   # last resort: which package manager exists?
        if   command -v apt-get >/dev/null 2>&1; then FAMILY=debian
        elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then FAMILY=rhel
        elif command -v pacman >/dev/null 2>&1; then FAMILY=arch
        elif command -v zypper >/dev/null 2>&1; then FAMILY=suse
        elif command -v apk >/dev/null 2>&1; then FAMILY=alpine
        elif command -v xbps-install >/dev/null 2>&1; then FAMILY=void
        elif command -v emerge >/dev/null 2>&1; then FAMILY=gentoo
        fi
    fi
    case "$FAMILY" in
        debian) PM=apt ;;
        rhel)   if command -v dnf >/dev/null 2>&1 || [ -n "$OS_RELEASE" ]; then PM=dnf; else PM=yum; fi ;;
        arch)   PM=pacman ;;
        suse)   PM=zypper ;;
        alpine) PM=apk ;;
        void)   PM=xbps ;;
        gentoo) PM=emerge ;;
        nixos)  PM=nix ;;
        *)      PM="" ;;
    esac
    if grep -qi microsoft /proc/version 2>/dev/null; then WSL=1; fi
    [ -n "$DISTRO_NAME" ] || DISTRO_NAME="Linux ($DISTRO_ID)"
}

# ------------------------------------------------------------------ packages
# BASE: Python 3 with venv + pip, git, CA certificates
# PDF:  Pango/HarfBuzz/fonts for WeasyPrint (optional)
# PDF_FALLBACK: older releases without the harfbuzz-subset package
packages_for() {
    BASE=""; PDF=""; PDF_FALLBACK=""; DOCKER_PKGS=""; PY_EXTRA=""
    case "$FAMILY" in
        debian)
            BASE="python3 python3-venv python3-pip git ca-certificates"
            PDF="libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core"
            PDF_FALLBACK="libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu-core"
            DOCKER_PKGS="docker.io"
            PY_EXTRA="python3.12 python3.12-venv python3.11 python3.11-venv" ;;
        rhel)
            BASE="python3 python3-pip git ca-certificates"
            PDF="pango fontconfig dejavu-sans-fonts"
            if [ "$DISTRO_ID" = "fedora" ]; then DOCKER_PKGS="moby-engine"; else DOCKER_PKGS="podman podman-docker"; fi
            PY_EXTRA="python3.12 python3.12-pip python3.11 python3.11-pip" ;;
        arch)
            BASE="python python-pip git ca-certificates"
            PDF="pango ttf-dejavu"
            DOCKER_PKGS="docker" ;;
        suse)
            BASE="python3 python3-pip git ca-certificates"
            PDF="pango fontconfig dejavu-fonts"
            DOCKER_PKGS="docker"
            PY_EXTRA="python312 python311" ;;
        alpine)
            BASE="python3 py3-pip git ca-certificates"
            PDF="pango fontconfig font-dejavu"
            DOCKER_PKGS="docker" ;;
        void)
            BASE="python3 python3-pip git ca-certificates"
            PDF="pango dejavu-fonts-ttf"
            DOCKER_PKGS="docker" ;;
        gentoo)
            BASE="dev-lang/python dev-vcs/git app-misc/ca-certificates"
            PDF="x11-libs/pango media-fonts/dejavu"
            DOCKER_PKGS="app-containers/docker" ;;
        macos)
            BASE="python git"
            PDF="pango"
            DOCKER_PKGS="--cask docker" ;;
        nixos)
            BASE="python3 git"
            PDF="pango" ;;
    esac
}

pm_install() {  # pm_install <packages...>; returns non-zero on failure
    case "$PM" in
        apt)    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@" ;;
        dnf)    as_root dnf install -y "$@" ;;
        yum)    as_root yum install -y "$@" ;;
        pacman) as_root pacman -S --needed --noconfirm "$@" ;;
        zypper) as_root zypper --non-interactive install --no-recommends "$@" ;;
        apk)    as_root apk add --no-cache "$@" ;;
        xbps)   as_root xbps-install -Sy "$@" ;;
        emerge) as_root emerge --noreplace "$@" ;;
        brew)   run brew install "$@" ;;
        *)      return 1 ;;
    esac
}

pm_refresh() {
    case "$PM" in
        apt) as_root apt-get update -q ;;
        *) : ;;   # dnf/zypper/apk refresh on install; pacman: we don't -Sy (no partial upgrades)
    esac
}

confirm() {
    [ "$ASSUME_YES" -eq 1 ] && return 0
    [ "$DRY_RUN" -eq 1 ] && return 0
    if [ ! -t 0 ]; then
        warn "Not a terminal and --yes not given: skipping system packages."
        return 1
    fi
    printf '%s  ? %s [Y/n] %s' "$C_B" "$1" "$C_0"
    read -r _ans || _ans=""
    case "$_ans" in n|N|no|NO) return 1 ;; *) return 0 ;; esac
}

install_system_packages() {
    packages_for
    case "$FAMILY" in
        nixos)
            warn "NixOS: system packages are declared, not installed. Use:"
            warn "    nix-shell -p $BASE $PDF"
            warn "or add them to configuration.nix, then re-run this script with --no-system."
            return 0 ;;
        unknown)
            warn "Unknown distribution - skipping system packages. Make sure Python >= 3.10 (with venv) and git are installed."
            return 0 ;;
    esac
    if [ "$PM" = "brew" ] && ! command -v brew >/dev/null 2>&1; then
        warn "Homebrew not found (https://brew.sh). Skipping system packages."
        return 0
    fi
    _pkgs="$BASE"
    say "System packages for $DISTRO_NAME via $PM: $_pkgs"
    [ "$WITH_PDF" -eq 1 ] && [ -n "$PDF" ] && printf '  PDF libraries: %s\n' "$PDF"
    confirm "Install them now?" || { warn "Skipped. Install them yourself if the next steps fail."; return 0; }
    pm_refresh || warn "Package index refresh failed (offline or mirror unreachable?)"
    # shellcheck disable=SC2086
    pm_install $_pkgs || die "Could not install: $_pkgs. Check your network/mirror, or install them manually and re-run with --no-system."
    if [ "$WITH_PDF" -eq 1 ] && [ -n "$PDF" ]; then
        # shellcheck disable=SC2086
        if ! pm_install $PDF; then
            if [ -n "$PDF_FALLBACK" ]; then
                # shellcheck disable=SC2086
                pm_install $PDF_FALLBACK || warn "PDF libraries not installed: PDF reports disabled (Markdown/HTML still work)."
            else
                warn "PDF libraries not installed: PDF reports disabled (Markdown/HTML still work)."
            fi
        fi
    fi
    if [ "$WITH_DOCKER" -eq 1 ] && [ -n "$DOCKER_PKGS" ]; then
        # shellcheck disable=SC2086
        if pm_install $DOCKER_PKGS; then
            if [ "$FAMILY" != "macos" ] && command -v systemctl >/dev/null 2>&1; then
                as_root systemctl enable --now docker 2>/dev/null || true
            fi
            if [ "$(id -u)" -ne 0 ] && [ "$FAMILY" != "macos" ]; then
                as_root usermod -aG docker "$(id -un)" 2>/dev/null || true
                warn "Log out and back in so your user can use Docker without sudo."
            fi
        else
            warn "Docker could not be installed; container jobs (image:) won't run."
        fi
    fi
}

# ------------------------------------------------------------------ python
py_ok() {  # usable: >= 3.10 and can create a venv with pip
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null || return 1
    "$1" -c 'import venv, ensurepip' 2>/dev/null || return 2
    return 0
}

find_python() {
    if [ -n "$PYTHON" ]; then
        _rc=0; py_ok "$PYTHON" || _rc=$?
        [ "$_rc" -eq 0 ] && return 0
        if [ "$_rc" -eq 2 ]; then
            case "$FAMILY" in
                debian) die "$PYTHON has no venv module. Install it: sudo apt install python3-venv (or python3.X-venv), or re-run without --no-system." ;;
                *) die "$PYTHON has no venv/ensurepip module. Install your distribution's python venv package." ;;
            esac
        fi
        die "$PYTHON is too old: Gatewright needs Python >= 3.10."
    fi
    _venv_missing=""
    for _p in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python; do
        _path=$(command -v "$_p" 2>/dev/null) || continue
        _rc=0; py_ok "$_path" || _rc=$?
        if [ "$_rc" -eq 0 ]; then PYTHON="$_path"; return 0; fi
        if [ "$_rc" -eq 2 ]; then _venv_missing="$_path"; fi
    done
    if [ -n "$_venv_missing" ]; then
        warn "$_venv_missing is new enough but has no venv/ensurepip module."
        [ "$FAMILY" = "debian" ] && warn "On $DISTRO_NAME that is the package python3-venv (or python3.X-venv)."
    fi
    return 1
}

# ------------------------------------------------------------------ main
detect_os
pick_sudo
say "Gatewright installer"
printf '  OS:        %s%s\n' "$DISTRO_NAME" "$([ "$WSL" -eq 1 ] && echo ' (WSL)')"
printf '  Family:    %s   package manager: %s\n' "$FAMILY" "${PM:-none}"
printf '  Install:   %s  (command: %s/gatewright)\n' "$PREFIX" "$BIN_DIR"
[ "$DRY_RUN" -eq 1 ] && printf '  %sDRY RUN: nothing will be changed%s\n' "$C_Y" "$C_0"
[ -f "$GATEWRIGHT_SRC/pyproject.toml" ] || die "Run install.sh from the Gatewright source folder (pyproject.toml not found in $GATEWRIGHT_SRC)."

packages_for
if [ "$DO_SYSTEM" -eq 1 ]; then
    install_system_packages
else
    say "Skipping system packages (--no-system)"
fi

if ! find_python; then
    if [ "$DO_SYSTEM" -eq 1 ] && [ -n "$PY_EXTRA" ]; then
        say "System Python is older than 3.10 - trying a newer Python package"
        for _pkg in $PY_EXTRA; do
            pm_install "$_pkg" >/dev/null 2>&1 || true
        done
    fi
    if [ "$DRY_RUN" -eq 1 ] && [ -z "$PYTHON" ]; then
        PYTHON="python3"; warn "No usable Python yet (would be installed above)."
    else
        find_python || die "No Python >= 3.10 with venv found. Install one (e.g. python3.12 + python3.12-venv) and re-run, or pass --python /path/to/python3.12."
    fi
fi
ok "Python: $PYTHON ($("$PYTHON" -c 'import sys; print(sys.version.split()[0])' 2>/dev/null || echo '?'))"

VENV="$PREFIX/venv"
say "Installing Gatewright into $VENV"
run mkdir -p "$PREFIX" "$BIN_DIR"
if [ -d "$VENV" ] && [ ! -x "$VENV/bin/python" ]; then run rm -rf "$VENV"; fi
if [ ! -x "$VENV/bin/python" ]; then
    run "$PYTHON" -m venv "$VENV" || die "Could not create a virtual environment with $PYTHON (see the error above). On Debian/Ubuntu/Kali install python3-venv; elsewhere make sure Python's ensurepip module is installed."
fi
if [ "$DRY_RUN" -eq 0 ] && ! "$VENV/bin/python" -c 'import sys' >/dev/null 2>&1; then
    die "The virtual environment in $VENV does not start (broken Python install?). Remove it and retry with another --python."
fi
if [ -n "$OFFLINE" ]; then
    [ -d "$OFFLINE" ] || die "--offline $OFFLINE: not a folder."
    PIP_NO_INDEX=1; 
    # pip splits this variable at spaces, so it has to be a URL
    PIP_FIND_LINKS="file://$(cd "$OFFLINE" && pwd | sed 's/ /%20/g')"
    export PIP_NO_INDEX PIP_FIND_LINKS
else
    run "$VENV/bin/python" -m pip install --quiet --upgrade pip || die "pip could not upgrade itself (network/proxy?)."
fi
_extras=""
[ "$WITH_PDF" -eq 1 ] && _extras="pdf"
[ "$WITH_TOOLS" -eq 1 ] && _extras="${_extras:+$_extras,}tools"
if [ -n "$_extras" ]; then _spec="${GATEWRIGHT_SRC}[$_extras]"; else _spec="$GATEWRIGHT_SRC"; fi
run "$VENV/bin/python" -m pip install --quiet "$_spec" || die "pip could not install Gatewright (network/proxy? see the error above)."
run ln -sf "$VENV/bin/gatewright" "$BIN_DIR/gatewright"

if [ "$DRY_RUN" -eq 0 ]; then
    {
        echo "installed=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "os=$OS_KIND"; echo "distro_id=$DISTRO_ID"; echo "distro=$DISTRO_NAME"
        echo "version=$DISTRO_VERSION"; echo "family=$FAMILY"; echo "package_manager=$PM"; echo "wsl=$WSL"
        echo "python=$PYTHON"; echo "venv=$VENV"; echo "source=$GATEWRIGHT_SRC"
    } > "$PREFIX/install-info.txt"

    say "Checking the installation"
    _ver=$("$BIN_DIR/gatewright" --version 2>&1) || die "gatewright does not start: $_ver"
    ok "$_ver"
    if "$VENV/bin/python" -c 'import textual, rich, psutil, yaml, jinja2' 2>/dev/null; then
        ok "terminal UI and engine libraries load"
    else
        die "Gatewright's libraries failed to import"
    fi
    if [ "$WITH_PDF" -eq 1 ]; then
        if "$VENV/bin/python" -c 'import weasyprint' >/dev/null 2>&1; then ok "PDF reports available"
        else warn "PDF reports unavailable (Pango missing). Markdown/HTML reports still work."; fi
    fi
fi

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)  warn "$BIN_DIR is not on your PATH. Add this line to ~/.bashrc or ~/.zshrc:"
        warn "    export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac
say "Done. Next: cd your-project && gatewright init && gatewright doctor && gatewright tui"
