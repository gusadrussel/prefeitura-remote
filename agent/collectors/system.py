"""Operating-system, firmware, and motherboard inventory."""

import getpass
import platform
import time
from pathlib import Path
from typing import Any

import psutil

from agent.collectors.common import as_list, powershell_json


def _linux_dmi_value(name: str) -> str | None:
    try:
        value = (Path("/sys/devices/virtual/dmi/id") / name).read_text().strip()
    except OSError:
        return None
    return value or None


def _windows_firmware() -> tuple[dict[str, Any], dict[str, Any]]:
    script = (
        "$board=Get-CimInstance Win32_BaseBoard; $bios=Get-CimInstance Win32_BIOS; "
        "[pscustomobject]@{board=[pscustomobject]@{manufacturer=$board.Manufacturer;"
        "model=$board.Product;serial=$board.SerialNumber};bios=[pscustomobject]@{"
        "vendor=$bios.Manufacturer;version=$bios.SMBIOSBIOSVersion;"
        "release_date=$bios.ReleaseDate;serial=$bios.SerialNumber}} | ConvertTo-Json -Compress"
    )
    result = powershell_json(script)
    if not isinstance(result, dict):
        return {}, {}
    return result.get("board") or {}, result.get("bios") or {}


def collect_system() -> dict[str, Any]:
    system_name = platform.system()
    result: dict[str, Any] = {
        "hostname": platform.node(),
        "logged_user": getpass.getuser(),
        "os_name": system_name,
        "os_version": platform.platform(),
        "kernel_version": platform.version(),
        "architecture": platform.machine(),
        "uptime_seconds": max(0, int(time.time() - psutil.boot_time())),
    }
    if system_name == "Windows":
        result["board"], result["bios"] = _windows_firmware()
    elif system_name == "Linux":
        result["board"] = {
            "manufacturer": _linux_dmi_value("board_vendor"),
            "model": _linux_dmi_value("board_name"),
            "serial": _linux_dmi_value("board_serial"),
        }
        result["bios"] = {
            "vendor": _linux_dmi_value("bios_vendor"),
            "version": _linux_dmi_value("bios_version"),
            "release_date": _linux_dmi_value("bios_date"),
            "serial": _linux_dmi_value("product_serial"),
        }
    else:
        result["board"] = {}
        result["bios"] = {}
    return result

