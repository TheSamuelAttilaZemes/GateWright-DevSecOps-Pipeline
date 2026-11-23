# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Hardware detection and resource budgeting.

The goal is to never take the machine down: jobs declare a cost (cpu, mem),
the scheduler only starts a job when the budget allows it, and a watchdog
kills the newest job if memory gets dangerously high anyway.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess  # nosec B404 - used only to query nvidia-smi
from dataclasses import asdict, dataclass

import psutil

from .models import fmt_bytes


@dataclass
class Hardware:
    """What this machine is. Stored in every run's manifest."""
    os: str
    arch: str
    hostname: str
    cpu_logical: int
    cpu_physical: int
    mem_total: int
    mem_available: int
    disk_free: int
    load_1m: float
    gpus: list[dict]
    in_container: bool
    on_battery: bool | None
    distro: str = ""
    os_family: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        gpu = f", {len(self.gpus)} GPU" if self.gpus else ""
        return (f"{self.cpu_logical} CPU, {fmt_bytes(self.mem_total)} RAM "
                f"({fmt_bytes(self.mem_available)} free), {fmt_bytes(self.disk_free)} disk free{gpu}")


@dataclass
class Budget:
    """How much of the machine jobs may use. Derived from Hardware + profile + limits."""
    profile: str
    max_parallel: int
    cpu: float                 # cpu units jobs may use concurrently
    mem: int                   # bytes jobs may use concurrently
    max_cpu_percent: float     # don't start new jobs above this live CPU %
    max_mem_percent: float     # don't start new jobs above this live RAM %
    kill_mem_percent: float    # watchdog kills newest job above this
    nice: int                  # process priority for jobs (posix)

    def to_dict(self) -> dict:
        return asdict(self)


def _gpus() -> list[dict]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run(  # nosec B603 - fixed argv, no shell
            [exe, "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    def mb(v: str) -> int | None:
        return int(v) if v.isdigit() else None   # "[N/A]" on some GPUs (iGPU, passthrough, MIG)

    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 3:
            gpus.append({"name": parts[0], "mem_total_mb": mb(parts[1]), "mem_used_mb": mb(parts[2])})
    return gpus


def detect(path: str = ".") -> Hardware:
    """Detect CPU, RAM, disk, GPU (nvidia-smi), battery, container and OS."""
    vm = psutil.virtual_memory()
    try:
        load = os.getloadavg()[0]
    except (OSError, AttributeError):
        load = psutil.cpu_percent(interval=0.1) / 100 * (psutil.cpu_count() or 1)
    try:
        bat = psutil.sensors_battery()
        on_battery = (not bat.power_plugged) if bat else None
    except (AttributeError, NotImplementedError):
        on_battery = None
    in_container = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
    from .platform_info import detect as detect_os
    osi = detect_os()
    return Hardware(
        os=f"{platform.system()} {platform.release()}", arch=platform.machine(),
        hostname=platform.node(), cpu_logical=psutil.cpu_count() or 1,
        cpu_physical=psutil.cpu_count(logical=False) or psutil.cpu_count() or 1,
        mem_total=vm.total, mem_available=vm.available,
        disk_free=shutil.disk_usage(path).free, load_1m=round(load, 2),
        gpus=_gpus(), in_container=in_container, on_battery=on_battery,
        distro=osi.name, os_family=osi.family,
    )


def pick_profile(hw: Hardware, requested: str = "auto") -> str:
    """'laptop' or 'server'. Machines with a battery are laptops; 8+ CPUs and
    16GB+ RAM without a battery is a server. A pipeline can force either.
    """
    if requested in ("laptop", "server"):
        return requested
    if hw.on_battery is not None:      # machines with a battery are laptops
        return "laptop"
    if hw.cpu_logical >= 8 and hw.mem_total >= 16 * 1024**3:
        return "server"
    return "laptop"


def make_budget(hw: Hardware, profile: str, limits: dict | None = None) -> Budget:
    """Turn hardware + profile into limits. A laptop keeps half the machine free
    for its user and lowers job priority; a server uses nearly everything.
    """
    limits = limits or {}
    if profile == "server":
        parallel = max(1, hw.cpu_logical - 1)
        cpu = max(1.0, hw.cpu_logical - 1.0)
        mem = int(hw.mem_available * 0.80)
        b = Budget(profile, parallel, cpu, mem, 90, 85, 95, 0)
    else:
        # leave half the machine to the human using it
        parallel = max(1, hw.cpu_logical // 2)
        cpu = max(1.0, hw.cpu_logical / 2)
        mem = int(hw.mem_available * 0.50)
        b = Budget(profile, parallel, cpu, mem, 80, 80, 92, 10)
    if hw.on_battery:
        b.max_parallel = max(1, b.max_parallel // 2)
    b.max_parallel = int(limits.get("max_parallel", b.max_parallel))
    b.max_cpu_percent = float(limits.get("max_cpu_percent", b.max_cpu_percent))
    b.max_mem_percent = float(limits.get("max_mem_percent", b.max_mem_percent))
    b.kill_mem_percent = float(limits.get("kill_mem_percent", b.kill_mem_percent))
    return b


def live_usage() -> dict:
    """Current system CPU %, RAM % and free RAM."""
    vm = psutil.virtual_memory()
    return {"cpu_percent": psutil.cpu_percent(interval=None), "mem_percent": vm.percent,
            "mem_available": vm.available}


def process_tree_usage(pid: int) -> tuple[int, float]:
    """(rss bytes, cpu percent) of a process and all children."""
    try:
        p = psutil.Process(pid)
        procs = [p, *p.children(recursive=True)]
    except psutil.Error:
        return 0, 0.0
    rss, cpu = 0, 0.0
    for pr in procs:
        try:
            rss += pr.memory_info().rss
            cpu += pr.cpu_percent(interval=None)
        except psutil.Error:
            pass
    return rss, cpu
