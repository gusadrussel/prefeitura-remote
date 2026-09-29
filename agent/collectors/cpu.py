"""Platform-aware CPU model and core-count collection."""

import platform
from pathlib import Path
from typing import Any

import psutil

from agent.collectors.common import as_list, powershell_json


def _int_or_none(value: Any) -> int | None:
	try:
		return int(value) if value is not None else None
	except (TypeError, ValueError):
		return None


def _linux_cpu_model(cpuinfo: str) -> str | None:
	for line in cpuinfo.splitlines():
		if line.lower().startswith(("model name", "hardware")):
			value = line.partition(":")[2].strip()
			if value:
				return value
	return None


def _sum_processor_counts(processors: list[dict[str, Any]], key: str) -> int | None:
	counts = [_int_or_none(processor.get(key)) for processor in processors]
	available_counts = [count for count in counts if count is not None]
	return sum(available_counts) if available_counts else None


def collect_cpu() -> dict[str, Any]:
	model = platform.processor().strip() or None
	physical_cores = None
	logical_cores = None
	system = platform.system()

	if system == "Windows":
		processors = as_list(
			powershell_json(
				"Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,"
				"NumberOfLogicalProcessors | ConvertTo-Json -Compress"
			)
		)
		names = list(dict.fromkeys(
			str(processor.get("Name")).strip()
			for processor in processors
			if processor.get("Name")
		))
		if names:
			model = "; ".join(names)
		physical_cores = _sum_processor_counts(processors, "NumberOfCores")
		logical_cores = _sum_processor_counts(processors, "NumberOfLogicalProcessors")
	elif system == "Linux":
		try:
			model = _linux_cpu_model(Path("/proc/cpuinfo").read_text()) or model
		except OSError:
			pass
		physical_cores = psutil.cpu_count(logical=False)
		logical_cores = psutil.cpu_count(logical=True)
	else:
		physical_cores = psutil.cpu_count(logical=False)
		logical_cores = psutil.cpu_count(logical=True)

	if physical_cores is None:
		physical_cores = psutil.cpu_count(logical=False)
	if logical_cores is None:
		logical_cores = psutil.cpu_count(logical=True)
	return {
		"model": model,
		"physical_cores": _int_or_none(physical_cores),
		"logical_cores": _int_or_none(logical_cores),
	}