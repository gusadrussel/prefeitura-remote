"""Network-interface, gateway, DNS, and basic connectivity inventory."""

import ipaddress
import platform
import socket
import struct
from pathlib import Path
from typing import Any

import psutil

from agent.collectors.common import as_list, powershell_json


def _parse_linux_routes(contents: str) -> list[str]:
    gateways = []
    for line in contents.splitlines()[1:]:
        fields = line.split()
        try:
            if len(fields) > 3 and fields[1] == "00000000" and int(fields[3], 16) & 1:
                gateway = socket.inet_ntoa(struct.pack("<I", int(fields[2], 16)))
                if gateway != "0.0.0.0" and gateway not in gateways:
                    gateways.append(gateway)
        except (ValueError, struct.error):
            continue
    return gateways


def _linux_routes() -> list[str]:
    try:
        return _parse_linux_routes(Path("/proc/net/route").read_text())
    except OSError:
        return []


def _is_loopback_interface(name: str, addresses: list[str]) -> bool:
    if name.lower() in {"lo", "loopback"}:
        return True
    for address in addresses:
        try:
            if ipaddress.ip_address(address.split("%", 1)[0]).is_loopback:
                return True
        except ValueError:
            continue
    return False


def collect_network() -> dict[str, Any]:
    stats = psutil.net_if_stats()
    interfaces = []
    for name, addresses in psutil.net_if_addrs().items():
        record: dict[str, Any] = {"name": name, "mac": None, "ipv4": [], "ipv6": []}
        for address in addresses:
            value = address.address
            if address.family == socket.AF_INET and value:
                record["ipv4"].append(value)
            elif address.family == socket.AF_INET6 and value:
                record["ipv6"].append(value.split("%", 1)[0])
            elif getattr(psutil, "AF_LINK", object()) == address.family:
                record["mac"] = value or None
        record["is_up"] = bool(stats.get(name) and stats[name].isup)
        interfaces.append(record)

    gateways: list[str] = []
    dns_servers: list[str] = []
    has_active_interface = any(
        item["is_up"]
        and not _is_loopback_interface(item["name"], item["ipv4"] + item["ipv6"])
        for item in interfaces
    )
    if platform.system() == "Linux":
        gateways = _linux_routes()
        try:
            for line in Path("/etc/resolv.conf").read_text().splitlines():
                fields = line.split()
                if len(fields) > 1 and fields[0] == "nameserver" and fields[1] not in dns_servers:
                    dns_servers.append(fields[1])
        except OSError:
            pass
    elif platform.system() == "Windows":
        settings = powershell_json(
            "Get-NetIPConfiguration | Select-Object -ExpandProperty IPv4DefaultGateway | "
            "Select-Object -ExpandProperty NextHop | ConvertTo-Json -Compress"
        )
        gateways = [str(item) for item in as_list(settings) if item]
        dns = powershell_json(
            "Get-DnsClientServerAddress | Select-Object -ExpandProperty ServerAddresses | "
            "ConvertTo-Json -Compress"
        )
        dns_servers = list(dict.fromkeys(str(item) for item in as_list(dns) if item))

    return {
        "hostname": socket.gethostname(),
        "interfaces": interfaces,
        "gateways": gateways,
        "dns_servers": dns_servers,
        "connectivity": {
            "has_active_interface": has_active_interface,
            "has_default_gateway": bool(gateways),
        },
    }