"""Collect physical disks and mounted-volume usage."""

import json
import platform
import shutil
import subprocess
from typing import Any

import psutil


def classify_rotational(value: Any, name: str = "", transport: str = "") -> str:
    """Normalize lsblk's boolean, numeric, and textual rotational values."""
    if str(transport).strip().lower() == "nvme" or "nvme" in name.lower():
        return "NVMe"
    if value is True or str(value).strip().lower() in {"1", "true", "yes"}:
        return "HDD"
    if value is False or str(value).strip().lower() in {"0", "false", "no"}:
        return "SSD"
    return "desconhecido"


def _linux_disks() -> list[dict[str, Any]]:
    if not shutil.which("lsblk"):
        return []

    try:
        result = subprocess.run(
            ["lsblk", "-b", "-J", "-d", "-o", "NAME,SIZE,MODEL,ROTA,TYPE,TRAN,SERIAL"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        devices = json.loads(result.stdout).get("blockdevices", [])
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []

    disks = []
    for device in devices:
        if device.get("type") != "disk":
            continue
        name = str(device.get("name") or "")
        disks.append(
            {
                "name": name,
                "model": device.get("model"),
                "serial": device.get("serial"),
                "size_bytes": _as_int(device.get("size")),
                "media_type": classify_rotational(
                    device.get("rota"), name, str(device.get("tran") or "")
                ),
                "interface": device.get("tran"),
            }
        )
    return disks


def _windows_disks() -> list[dict[str, Any]]:
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if not executable:
        return []

    script = (
        "$physical=Get-PhysicalDisk -ErrorAction SilentlyContinue | Select-Object "
        "FriendlyName,SerialNumber,Size,MediaType,BusType; "
        "if($physical){$physical | ConvertTo-Json -Compress} else {"
        "Get-CimInstance Win32_DiskDrive | Select-Object DeviceID,Model,SerialNumber,"
        "Size,MediaType,InterfaceType | ConvertTo-Json -Compress}"
    )
    try:
        result = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        devices = json.loads(result.stdout or "[]")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []

    if isinstance(devices, dict):
        devices = [devices]
    return [
        {
            "name": device.get("DeviceID") or device.get("FriendlyName"),
            "model": device.get("Model") or device.get("FriendlyName"),
            "serial": device.get("SerialNumber"),
            "size_bytes": _as_int(device.get("Size")),
            "media_type": _windows_media_type(
                device.get("MediaType"), device.get("BusType") or device.get("InterfaceType")
            ),
            "interface": device.get("InterfaceType") or device.get("BusType"),
        }
        for device in devices
    ]


def _windows_media_type(value: Any, bus_type: Any = None) -> str:
    media_type = str(value or "").lower()
    if "nvme" in str(bus_type or "").lower():
        return "NVMe"
    if "nvme" in media_type:
        return "NVMe"
    if "solid state" in media_type or "ssd" in media_type:
        return "SSD"
    if media_type in {"hdd", "rotational", "hard disk"}:
        return "HDD"
    return "desconhecido"


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def collect_storage() -> dict[str, list[dict[str, Any]]]:
    """Return physical disks and mounted volumes, tolerating unavailable data."""
    system = platform.system()
    if system == "Linux":
        disks = _linux_disks()
    elif system == "Windows":
        disks = _windows_disks()
    else:
        disks = []

    volumes = []
    for partition in psutil.disk_partitions(all=False):
        filesystem = (partition.fstype or "").lower()
        mountpoint = partition.mountpoint or ""
        if not mountpoint:
            continue
        if system == "Linux" and filesystem in {
            "squashfs", "tmpfs", "devtmpfs", "overlay", "proc", "sysfs", "cgroup2"
        }:
            continue
        if system == "Linux" and mountpoint.startswith("/snap/"):
            continue
        try:
            usage = psutil.disk_usage(mountpoint)
        except (OSError, PermissionError):
            continue
        volumes.append(
            {
                "device": partition.device,
                "mountpoint": mountpoint,
                "filesystem": filesystem or None,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
            }
        )
    return {"disks": disks, "volumes": volumes}