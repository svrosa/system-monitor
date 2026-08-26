from __future__ import annotations
import platform
import socket
import os
import subprocess
import time
from pathlib import Path

import psutil


def human_bytes(value: float) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(value)

    for unit in units:
        if abs(value) < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{value:.0f} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024

    return f"{value:.1f} TiB"


def uptime_string() -> str:
    seconds = int(time.time() - psutil.boot_time())
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    if days:
        return f"{days}d {hours:02}:{minutes:02}:{seconds:02}"

    return f"{hours:02}:{minutes:02}:{seconds:02}"


def cpu_temperature() -> float | None:
    try:
        temperatures = psutil.sensors_temperatures()
    except Exception:
        return None

    coretemp = temperatures.get("coretemp", [])

    for sensor in coretemp:
        if sensor.label == "Package id 0":
            return sensor.current

    if coretemp:
        return max(sensor.current for sensor in coretemp)

    return None


def cpu_metrics() -> dict:
    frequency = psutil.cpu_freq()

    return {
        "percent": psutil.cpu_percent(interval=None),
        "per_core": psutil.cpu_percent(interval=None, percpu=True),
        "cores": psutil.cpu_count(logical=False),
        "threads": psutil.cpu_count(logical=True),
        "frequency": frequency.current if frequency else None,
        "temperature": cpu_temperature(),
    }


def memory_metrics() -> dict:
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()

    return {
        "percent": memory.percent,
        "used": memory.used,
        "total": memory.total,
        "swap_used": swap.used,
        "swap_total": swap.total,
        "swap_percent": swap.percent,
    }


def disk_metrics() -> list[dict]:
    disks = []

    targets = [
        ("Root", "/"),
        ("HDD", "/mnt/HDD"),
    ]

    for name, mountpoint in targets:
        if not Path(mountpoint).exists():
            continue

        usage = psutil.disk_usage(mountpoint)

        disks.append(
            {
                "name": name,
                "mountpoint": mountpoint,
                "percent": usage.percent,
                "used": usage.used,
                "total": usage.total,
            }
        )

    return disks


def gpu_metrics() -> dict | None:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,temperature.gpu,utilization.gpu,"
                "memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=1,
            check=True,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None

    line = result.stdout.strip().splitlines()

    if not line:
        return None

    try:
        name, temperature, utilization, memory_total, memory_used = [
            value.strip() for value in line[0].split(",")
        ]

        return {
            "name": name,
            "temperature": float(temperature),
            "percent": float(utilization),
            "memory_total": float(memory_total) * 1024 * 1024,
            "memory_used": float(memory_used) * 1024 * 1024,
        }
    except (ValueError, IndexError):
        return None


def gpu_process_metrics() -> dict[int, dict]:
    """Return NVIDIA per-process GPU utilization and VRAM usage."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "pmon",
                "-c",
                "1",
                "-s",
                "um",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return {}

    processes: dict[int, dict] = {}

    for line in result.stdout.splitlines():
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        fields = line.split()

        # Expected:
        # gpu pid type sm mem enc dec jpg ofa fb ccpm command
        if len(fields) < 11:
            continue

        try:
            pid = int(fields[1])
        except ValueError:
            continue

        sm_text = fields[3]
        fb_text = fields[9]

        gpu = None
        if sm_text != "-":
            try:
                gpu = float(sm_text)
            except ValueError:
                pass

        vram = 0
        if fb_text != "-":
            try:
                vram = int(fb_text) * 1024 * 1024
            except ValueError:
                pass

        processes[pid] = {
            "gpu": gpu,
            "vram": vram,
        }

    return processes


def system_metrics() -> dict:
    load1, load5, load15 = os.getloadavg()

    return {
        "uptime": uptime_string(),
        "hostname": socket.gethostname(),
        "kernel": platform.release(),
        "load": (load1, load5, load15),
        "processes": len(psutil.pids()),
    }


def default_network_interface() -> str | None:
    """Return the interface used by the default IPv4 route."""
    try:
        with open("/proc/net/route", encoding="utf-8") as route_file:
            next(route_file, None)

            for line in route_file:
                fields = line.split()

                if len(fields) >= 4 and fields[1] == "00000000":
                    return fields[0]
    except OSError:
        pass

    return None


def interface_ipv4(interface: str | None) -> str | None:
    if not interface:
        return None

    for address in psutil.net_if_addrs().get(interface, []):
        if address.family == socket.AF_INET:
            return address.address

    return None

class NetworkSampler:
    def __init__(self) -> None:
        self.interface = default_network_interface()

        counters = self._counters()

        self._received = counters.bytes_recv
        self._sent = counters.bytes_sent
        self._time = time.monotonic()

    def _counters(self):
        if self.interface:
            per_nic = psutil.net_io_counters(pernic=True)

            if self.interface in per_nic:
                return per_nic[self.interface]

        return psutil.net_io_counters()

    def sample(self) -> dict:
        current_interface = default_network_interface()

        # Handle switching Wi-Fi/Ethernet/VPN interfaces cleanly.
        if current_interface != self.interface:
            self.interface = current_interface

            counters = self._counters()

            self._received = counters.bytes_recv
            self._sent = counters.bytes_sent
            self._time = time.monotonic()

            return {
                "download": 0.0,
                "upload": 0.0,
                "received_total": counters.bytes_recv,
                "sent_total": counters.bytes_sent,
                "interface": self.interface or "unknown",
                "ip": interface_ipv4(self.interface) or "N/A",
            }

        counters = self._counters()
        now = time.monotonic()

        elapsed = max(now - self._time, 0.001)

        received = (counters.bytes_recv - self._received) / elapsed
        sent = (counters.bytes_sent - self._sent) / elapsed

        self._received = counters.bytes_recv
        self._sent = counters.bytes_sent
        self._time = now

        return {
            "download": max(received, 0),
            "upload": max(sent, 0),
            "received_total": counters.bytes_recv,
            "sent_total": counters.bytes_sent,
            "interface": self.interface or "unknown",
            "ip": interface_ipv4(self.interface) or "N/A",
        }


def prime_process_cpu() -> None:
    for process in psutil.process_iter():
        try:
            process.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


def process_snapshot() -> list[dict]:
    rows = []
    gpu_processes = gpu_process_metrics()

    for process in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            cpu = process.cpu_percent(None)

            memory_info = process.info["memory_info"]
            rss = memory_info.rss if memory_info else 0

            gpu_info = gpu_processes.get(process.info["pid"], {})

            rows.append(
                {
                    "pid": process.info["pid"],
                    "name": process.info["name"] or "?",
                    "cpu": cpu,
                    "gpu": gpu_info.get("gpu"),
                    "vram": gpu_info.get("vram", 0),
                    "rss": rss,
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return rows
    