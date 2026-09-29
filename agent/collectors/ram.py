"""Memory capacity and optional per-module details."""

import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import psutil

from agent.collectors.common import as_list, powershell_json


def _capacity_bytes(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    match = re.fullmatch(r"([\d.]+)\s*(KB|MB|GB|TB|KiB|MiB|GiB|TiB)?", text, re.I)
    if not match:
        return None
    amount = float(match.group(1))
    unit = (match.group(2) or "B").upper()
    # dmidecode reports SMBIOS memory sizes in binary MB/GB despite the labels.
    factors = {
        "B": 1,
        "KB": 1024,
        "MB": 1024**2,
        "GB": 1024**3,
        "TB": 1024**4,
        "KIB": 1024,
        "MIB": 1024**2,
        "GIB": 1024**3,
        "TIB": 1024**4,
    }
    return int(amount * factors[unit])


def _windows_modules() -> list[dict[str, Any]]:
    script = (
        "Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity,Manufacturer,"
        "PartNumber,Speed,ConfiguredClockSpeed,SMBIOSMemoryType | ConvertTo-Json -Compress"
    )
    modules = []
    ddr_types = {
        20: "DDR",
        21: "DDR2",
        22: "DDR2 FB-DIMM",
        24: "DDR3",
        25: "FBD2",
        26: "DDR4",
        27: "LPDDR",
        28: "LPDDR2",
        29: "LPDDR3",
        30: "LPDDR4",
        34: "DDR5",
        35: "LPDDR5",
    }
    for item in as_list(powershell_json(script)):
        memory_type = item.get("SMBIOSMemoryType")
        try:
            memory_type = ddr_types.get(int(memory_type), str(memory_type or "desconhecido"))
        except (TypeError, ValueError):
            memory_type = "desconhecido"
        modules.append(
            {
                "capacity_bytes": _capacity_bytes(item.get("Capacity")),
                "type": memory_type,
                "speed_mhz": _int_or_none(item.get("ConfiguredClockSpeed") or item.get("Speed")),
                "manufacturer": _clean(item.get("Manufacturer")),
                "part_number": _clean(item.get("PartNumber")),
            }
        )
    return modules


def _linux_modules() -> list[dict[str, Any]]:
    executable = shutil.which("dmidecode")
    if not executable:
        return []
    try:
        result = subprocess.run(
            [executable, "--type", "memory"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except PermissionError:
        result = None
    except subprocess.SubprocessError:
        return []

    if result is None or (
        result.returncode != 0
        and any(
            message in f"{result.stdout}\n{result.stderr}".lower()
            for message in ("permission denied", "can't read memory", "could not access /dev/mem")
        )
    ):
        sudo = shutil.which("sudo")
        if not sudo:
            return []
        try:
            result = subprocess.run(
                [sudo, "-n", executable, "--type", "memory"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return []
    elif result.returncode != 0:
        return []

    modules = []
    current: dict[str, Any] | None = None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped == "Memory Device":
            if current and current.get("capacity_bytes"):
                modules.append(current)
            current = {}
        elif current is not None and ":" in stripped:
            key, _, value = stripped.partition(":")
            value = value.strip()
            if key == "Size":
                current["capacity_bytes"] = _capacity_bytes(value)
            elif key == "Type":
                current["type"] = value if value not in {"", "Unknown"} else "desconhecido"
            elif key == "Speed":
                current["speed_mhz"] = _speed_mhz(value)
            elif key == "Manufacturer":
                current["manufacturer"] = _clean(value)
            elif key == "Part Number":
                current["part_number"] = _clean(value)
    if current and current.get("capacity_bytes"):
        modules.append(current)
    return modules


def collect_memory() -> dict[str, Any]:
    memory = psutil.virtual_memory()
    system = platform.system()
    modules = _windows_modules() if system == "Windows" else _linux_modules() if system == "Linux" else []
    return {
        "total_bytes": memory.total,
        "available_bytes": memory.available,
        "modules": modules,
    }


def _speed_mhz(value: str) -> int | None:
    match = re.search(r"(\d+)\s*MT/s", value, re.I)
    return int(match.group(1)) if match else _int_or_none(value.split()[0] if value else None)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None