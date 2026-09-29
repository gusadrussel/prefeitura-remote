"""Video-controller inventory without requiring administrative privileges."""

import platform
import shlex
import shutil
import subprocess
from typing import Any

from agent.collectors.common import as_list, powershell_json


def collect_gpus() -> list[dict[str, Any]]:
    if platform.system() == "Windows":
        result = powershell_json(
            "Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,"
            "AdapterRAM | ConvertTo-Json -Compress"
        )
        gpus = []
        for item in as_list(result):
            try:
                memory_bytes = int(item.get("AdapterRAM")) if item.get("AdapterRAM") else None
            except (TypeError, ValueError):
                memory_bytes = None
            gpus.append(
                {
                    "name": item.get("Name"),
                    "driver_version": item.get("DriverVersion"),
                    "memory_bytes": memory_bytes,
                }
            )
        return gpus

    if platform.system() != "Linux":
        return []
    executable = shutil.which("lspci")
    if not executable:
        return []
    try:
        result = subprocess.run(
            [executable, "-mm"], capture_output=True, text=True, check=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return []
    devices = []
    for line in result.stdout.splitlines():
        if '"VGA compatible controller"' in line or '"3D controller"' in line:
            try:
                fields = shlex.split(line)
            except ValueError:
                continue
            if len(fields) >= 4:
                devices.append({"name": f"{fields[2]} {fields[3]}", "address": fields[0]})
    return devices