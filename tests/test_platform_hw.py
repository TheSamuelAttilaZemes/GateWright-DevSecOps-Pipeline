# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""OS detection and hardware detection branches that a single test machine can't reach by itself."""

import subprocess
import sys

import psutil
import pytest

from gatewright import hardware, platform_info
from gatewright.hardware import Hardware, make_budget, pick_profile


def none(_):
    return None


def test_parse_os_release_skips_comments_and_junk():
    d = platform_info.parse_os_release("# c\n\nJUNK\nID=kali\nNAME='Kali'\nX=\"\n")
    assert d == {"ID": "kali", "NAME": "Kali", "X": '"'}


@pytest.mark.parametrize("did, like, which, fam", [
    ("x", ["rhel9"], none, "rhel"), ("x", ["redhat"], none, "rhel"), ("x", ["suse-ish"], none, "suse"),
    ("x", ["unknownlike"], lambda n: "/x" if n == "pacman" else None, "arch"),
    ("x", [], none, "unknown")])
def test_family_fallbacks(did, like, which, fam):
    assert platform_info.family_for(did, like, which) == fam


def test_detect_macos_and_windows(monkeypatch):
    monkeypatch.setattr(platform_info.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform_info.platform, "mac_ver", lambda: ("15.1", ("", "", ""), ""))
    i = platform_info.detect(which=lambda n: "/opt/homebrew/bin/brew")
    assert i.family == "macos" and i.package_manager == "brew" and i.to_dict()["name"] == "macOS 15.1"
    monkeypatch.setattr(platform_info.platform, "system", lambda: "Windows")
    i = platform_info.detect(which=none)
    assert i.family == "windows" and i.package_manager == ""


def test_detect_missing_os_release_and_yum_only(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_info.platform, "system", lambda: "Linux")
    i = platform_info.detect(str(tmp_path / "missing"), which=none)
    assert i.family == "unknown" and i.name == "Linux"
    (tmp_path / "os").write_text("ID=centos\nVERSION_ID=7\n")
    i = platform_info.detect(str(tmp_path / "os"), which=lambda n: "/usr/bin/yum" if n == "yum" else None)
    assert i.family == "rhel" and i.package_manager == "yum"


def test_detect_survives_bad_bytes_and_names_other_unixes(tmp_path, monkeypatch):
    (tmp_path / "os").write_bytes(b"ID=freebsd\nNAME=Free\xff\xfeBSD\n")
    monkeypatch.setattr(platform_info.platform, "system", lambda: "FreeBSD")
    i = platform_info.detect(str(tmp_path / "os"), which=none)      # regression: UnicodeDecodeError
    assert i.system == "FreeBSD" and i.distro_id == "freebsd"


def test_detect_without_proc_version(monkeypatch, tmp_path):
    real_open = open

    def fake_open(path, *a, **k):
        if path == "/proc/version":
            raise OSError("no /proc")
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", fake_open)
    monkeypatch.setattr(platform_info.platform, "system", lambda: "Linux")
    assert platform_info.detect(which=none).wsl is False


def test_pip_tool_hints(monkeypatch):
    info = platform_info.detect()
    monkeypatch.setattr(sys, "prefix", "/venv")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    assert "-m pip install ruff" in platform_info.install_hint("ruff", info)
    monkeypatch.setattr(sys, "prefix", "/usr")
    info.externally_managed = True
    assert platform_info.install_hint("ruff", info) == "pipx install ruff"
    info.externally_managed = False
    assert platform_info.install_hint("ruff", info) == "pip install ruff"


# ---------------------------------------------------------------- hardware
def _run_returning(stdout):
    return lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=stdout, stderr="")


def test_gpu_parsing(monkeypatch):
    monkeypatch.setattr(hardware.shutil, "which", lambda n: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(hardware.subprocess, "run", _run_returning("RTX 4090, 24564, 512\nT4, [N/A], [N/A]\nbad line\n"))
    g = hardware._gpus()                                   # regression: "[N/A]" crashed detection
    assert g == [{"name": "RTX 4090", "mem_total_mb": 24564, "mem_used_mb": 512},
                 {"name": "T4", "mem_total_mb": None, "mem_used_mb": None}]

    def broken(*a, **k):
        raise subprocess.TimeoutExpired("nvidia-smi", 5)
    monkeypatch.setattr(hardware.subprocess, "run", broken)
    assert hardware._gpus() == []


def test_detect_without_loadavg_or_battery_sensor(monkeypatch, tmp_path):
    def no_loadavg():
        raise OSError

    def no_sensor():
        raise NotImplementedError
    monkeypatch.setattr(hardware.os, "getloadavg", no_loadavg, raising=False)
    monkeypatch.setattr(hardware.psutil, "sensors_battery", no_sensor, raising=False)
    hw = hardware.detect(str(tmp_path))
    assert hw.on_battery is None and hw.load_1m >= 0


def _hw(**kw):
    base = dict(os="Linux", arch="x86_64", hostname="h", cpu_logical=16, cpu_physical=8, mem_total=64 * 1024**3,
                mem_available=32 * 1024**3, disk_free=1, load_1m=0.1, gpus=[], in_container=False, on_battery=None,
                distro="", os_family="debian")
    base.update(kw)
    return Hardware(**base)


def test_profiles_and_battery_budget():
    assert pick_profile(_hw()) == "server"
    assert pick_profile(_hw(on_battery=False)) == "laptop"
    assert pick_profile(_hw(cpu_logical=4)) == "laptop"
    assert make_budget(_hw(on_battery=True), "laptop").max_parallel == 4      # 16//2 halved on battery


def test_process_tree_usage_of_missing_and_vanishing_processes(monkeypatch):
    assert hardware.process_tree_usage(2**22 + 12345) == (0, 0.0)

    class Gone:
        def children(self, recursive):
            return []

        def memory_info(self):
            raise psutil.NoSuchProcess(1)
    monkeypatch.setattr(hardware.psutil, "Process", lambda pid: Gone())
    assert hardware.process_tree_usage(1) == (0, 0.0)
