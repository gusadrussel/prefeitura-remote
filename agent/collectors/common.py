"""Helpers shared by platform-specific collectors."""

import json
import shutil
import subprocess
from typing import Any


def powershell_json(script: str) -> Any:
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        return json.loads(result.stdout) if result.stdout.strip() else None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]