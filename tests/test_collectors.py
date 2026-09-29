import json
import socket
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.collectors import common, cpu, disk, gpu, network, ram, system


class CpuCollectorTests(unittest.TestCase):
    def test_windows_counts_all_processor_packages(self):
        processors = [
            {"Name": "CPU A", "NumberOfCores": 4, "NumberOfLogicalProcessors": 8},
            {"Name": "CPU B", "NumberOfCores": 2, "NumberOfLogicalProcessors": 4},
        ]
        with (
            patch.object(cpu.platform, "system", return_value="Windows"),
            patch.object(cpu.platform, "processor", return_value=""),
            patch.object(cpu, "powershell_json", return_value=processors),
            patch.object(cpu.psutil, "cpu_count", return_value=1),
        ):
            result = cpu.collect_cpu()
        self.assertEqual(result, {
            "model": "CPU A; CPU B",
            "physical_cores": 6,
            "logical_cores": 12,
        })

    def test_linux_cpuinfo_parser_handles_model_and_hardware_keys(self):
        self.assertEqual(
            cpu._linux_cpu_model("processor: 0\nmodel name: Test CPU\n"), "Test CPU"
        )
        self.assertEqual(cpu._linux_cpu_model("Hardware: ARM Test\n"), "ARM Test")
        self.assertIsNone(cpu._linux_cpu_model("processor: 0\n"))


class DiskCollectorTests(unittest.TestCase):
    def test_missing_lsblk_returns_empty_list(self):
        with patch.object(disk.shutil, "which", return_value=None):
            self.assertEqual(disk._linux_disks(), [])

    def test_permission_error_skips_unreadable_volume(self):
        partition = SimpleNamespace(fstype="ext4", mountpoint="/restricted", device="/dev/test")
        with (
            patch.object(disk.platform, "system", return_value="Linux"),
            patch.object(disk, "_linux_disks", return_value=[]),
            patch.object(disk.psutil, "disk_partitions", return_value=[partition]),
            patch.object(disk.psutil, "disk_usage", side_effect=PermissionError),
        ):
            result = disk.collect_storage()
        self.assertEqual(result, {"disks": [], "volumes": []})

    def test_windows_cim_disk_fields_keep_types_and_unknown_media(self):
        result = SimpleNamespace(
            stdout=json.dumps({
                "FriendlyName": "Disk 0",
                "SerialNumber": None,
                "Size": "500000000000",
                "MediaType": "Fixed hard disk media",
                "BusType": "NVMe",
            })
        )
        with (
            patch.object(disk.shutil, "which", return_value="powershell.exe"),
            patch.object(disk.subprocess, "run", return_value=result),
        ):
            disks = disk._windows_disks()
        self.assertEqual(disks[0]["size_bytes"], 500_000_000_000)
        self.assertEqual(disks[0]["media_type"], "NVMe")
        self.assertIsNone(disks[0]["serial"])

    def test_missing_filesystem_type_is_tolerated(self):
        partition = SimpleNamespace(fstype=None, mountpoint="/volume", device="/dev/test")
        usage = SimpleNamespace(total=100, used=40, free=60)
        with (
            patch.object(disk.platform, "system", return_value="Linux"),
            patch.object(disk, "_linux_disks", return_value=[]),
            patch.object(disk.psutil, "disk_partitions", return_value=[partition]),
            patch.object(disk.psutil, "disk_usage", return_value=usage),
        ):
            result = disk.collect_storage()
        self.assertIsNone(result["volumes"][0]["filesystem"])
        self.assertEqual(result["volumes"][0]["total_bytes"], 100)


class GpuCollectorTests(unittest.TestCase):
    def test_linux_lspci_parser_preserves_vendor_and_model(self):
        output = (
            '00:02.0 "VGA compatible controller" "Intel Corporation" '
            '"HD Graphics 620" -r02\n'
            '01:00.0 "3D controller" "NVIDIA Corporation" "GeForce Test" -ra2\n'
        )
        completed = SimpleNamespace(stdout=output)
        with (
            patch.object(gpu.platform, "system", return_value="Linux"),
            patch.object(gpu.shutil, "which", return_value="/usr/bin/lspci"),
            patch.object(gpu.subprocess, "run", return_value=completed),
        ):
            result = gpu.collect_gpus()
        self.assertEqual(result[0], {
            "name": "Intel Corporation HD Graphics 620", "address": "00:02.0"
        })
        self.assertIn("GeForce Test", result[1]["name"])

    def test_missing_lspci_returns_empty_list(self):
        with (
            patch.object(gpu.platform, "system", return_value="Linux"),
            patch.object(gpu.shutil, "which", return_value=None),
        ):
            self.assertEqual(gpu.collect_gpus(), [])

    def test_windows_cim_invalid_memory_is_none_not_an_exception(self):
        with (
            patch.object(gpu.platform, "system", return_value="Windows"),
            patch.object(gpu, "powershell_json", return_value={
                "Name": "Test GPU", "DriverVersion": None, "AdapterRAM": "unknown"
            }),
        ):
            result = gpu.collect_gpus()
        self.assertEqual(result[0]["name"], "Test GPU")
        self.assertIsNone(result[0]["memory_bytes"])
        self.assertIsNone(result[0]["driver_version"])


class NetworkCollectorTests(unittest.TestCase):
    def test_linux_route_parser_ignores_down_default_routes(self):
        contents = (
            "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
            "eth0 00000000 0102A8C0 0001 0 0 100 00000000 0 0 0\n"
            "wlan0 00000000 0202A8C0 0000 0 0 200 00000000 0 0 0\n"
        )
        self.assertEqual(network._parse_linux_routes(contents), ["192.168.2.1"])

    def test_loopback_alone_is_not_reported_as_active_network(self):
        addresses = {
            "lo": [SimpleNamespace(family=socket.AF_INET, address="127.0.0.1")]
        }
        with (
            patch.object(network.platform, "system", return_value="Other"),
            patch.object(network.psutil, "net_if_stats", return_value={"lo": SimpleNamespace(isup=True)}),
            patch.object(network.psutil, "net_if_addrs", return_value=addresses),
        ):
            result = network.collect_network()
        self.assertTrue(result["interfaces"][0]["is_up"])
        self.assertFalse(result["connectivity"]["has_active_interface"])

    def test_windows_gateways_and_dns_are_normalized_and_deduplicated(self):
        addresses = {
            "Ethernet": [SimpleNamespace(family=socket.AF_INET, address="10.0.0.2")]
        }
        with (
            patch.object(network.platform, "system", return_value="Windows"),
            patch.object(network.psutil, "net_if_stats", return_value={"Ethernet": SimpleNamespace(isup=True)}),
            patch.object(network.psutil, "net_if_addrs", return_value=addresses),
            patch.object(network, "powershell_json", side_effect=["10.0.0.1", ["1.1.1.1", "1.1.1.1"]]),
        ):
            result = network.collect_network()
        self.assertEqual(result["gateways"], ["10.0.0.1"])
        self.assertEqual(result["dns_servers"], ["1.1.1.1"])
        self.assertTrue(result["connectivity"]["has_active_interface"])


class RamCollectorTests(unittest.TestCase):
    def test_dmidecode_megabytes_use_binary_units(self):
        self.assertEqual(ram._capacity_bytes("8192 MB"), 8 * 1024**3)
        self.assertEqual(ram._capacity_bytes("8 GiB"), 8 * 1024**3)
        self.assertIsNone(ram._capacity_bytes("No Module Installed"))

    def test_total_and_available_memory_remain_distinct_integers(self):
        memory = SimpleNamespace(total=8 * 1024**3, available=3 * 1024**3)
        with (
            patch.object(ram.platform, "system", return_value="Linux"),
            patch.object(ram.psutil, "virtual_memory", return_value=memory),
            patch.object(ram, "_linux_modules", return_value=[]),
        ):
            result = ram.collect_memory()
        self.assertEqual(result["total_bytes"], 8 * 1024**3)
        self.assertEqual(result["available_bytes"], 3 * 1024**3)
        self.assertIsInstance(result["total_bytes"], int)
        self.assertIsInstance(result["available_bytes"], int)

    def test_missing_or_denied_dmidecode_returns_no_modules(self):
        with patch.object(ram.shutil, "which", return_value=None):
            self.assertEqual(ram._linux_modules(), [])
        with (
            patch.object(
                ram.shutil,
                "which",
                side_effect=lambda name: {"dmidecode": "/usr/sbin/dmidecode"}.get(name),
            ),
            patch.object(ram.subprocess, "run", side_effect=PermissionError),
        ):
            self.assertEqual(ram._linux_modules(), [])

    def test_linux_dmi_parser_recovers_two_four_gib_modules(self):
        output = """Handle 0x0010, DMI type 17, 40 bytes
Memory Device
	Size: 4096 MB
	Type: DDR4
	Speed: 2133 MT/s
	Manufacturer: Test Vendor

Handle 0x0011, DMI type 17, 40 bytes
Memory Device
	Size: 4096 MB
	Type: DDR4
	Speed: 2133 MT/s
	Manufacturer: Test Vendor
"""
        command_result = SimpleNamespace(returncode=0, stdout=output, stderr="")
        with (
            patch.object(
                ram.shutil,
                "which",
                side_effect=lambda name: {"dmidecode": "/usr/sbin/dmidecode"}.get(name),
            ),
            patch.object(ram.subprocess, "run", return_value=command_result),
        ):
            modules = ram._linux_modules()
        self.assertEqual(len(modules), 2)
        self.assertEqual([module["capacity_bytes"] for module in modules], [4 * 1024**3] * 2)
        self.assertEqual([module["type"] for module in modules], ["DDR4", "DDR4"])
        self.assertEqual([module["speed_mhz"] for module in modules], [2133, 2133])

    def test_permission_failure_uses_only_noninteractive_sudo_fallback(self):
        denied = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="/sys/firmware/dmi/tables/smbios_entry_point: Permission denied",
        )
        success = SimpleNamespace(returncode=0, stdout="Memory Device\n\tSize: 4 GB\n", stderr="")
        which = lambda name: {
            "dmidecode": "/usr/sbin/dmidecode",
            "sudo": "/usr/bin/sudo",
        }.get(name)
        with (
            patch.object(ram.shutil, "which", side_effect=which),
            patch.object(ram.subprocess, "run", side_effect=[denied, success]) as run,
        ):
            modules = ram._linux_modules()
        self.assertEqual(len(modules), 1)
        self.assertEqual(run.call_args_list[1].args[0][:2], ["/usr/bin/sudo", "-n"])

    def test_windows_cim_memory_module_normalizes_ddr_type_and_optional_values(self):
        item = {
            "Capacity": 8 * 1024**3,
            "Manufacturer": None,
            "PartNumber": "PART-1",
            "Speed": 2666,
            "ConfiguredClockSpeed": 3200,
            "SMBIOSMemoryType": 26,
        }
        with patch.object(ram, "powershell_json", return_value=item):
            modules = ram._windows_modules()
        self.assertEqual(modules[0]["capacity_bytes"], 8 * 1024**3)
        self.assertEqual(modules[0]["type"], "DDR4")
        self.assertEqual(modules[0]["speed_mhz"], 3200)
        self.assertIsNone(modules[0]["manufacturer"])


class SystemCollectorTests(unittest.TestCase):
    def test_denied_dmi_file_returns_none(self):
        with patch.object(Path, "read_text", side_effect=PermissionError):
            self.assertIsNone(system._linux_dmi_value("board_name"))

    def test_unsupported_platform_has_typed_firmware_fallbacks(self):
        with (
            patch.object(system.platform, "system", return_value="Other"),
            patch.object(system.platform, "node", return_value="host-test"),
            patch.object(system.platform, "platform", return_value="Other OS"),
            patch.object(system.platform, "version", return_value="1"),
            patch.object(system.platform, "machine", return_value="x86_64"),
            patch.object(system.getpass, "getuser", return_value="user-test"),
            patch.object(system.psutil, "boot_time", return_value=100),
            patch.object(system.time, "time", return_value=200),
        ):
            result = system.collect_system()
        self.assertEqual(result["uptime_seconds"], 100)
        self.assertIsInstance(result["uptime_seconds"], int)
        self.assertEqual(result["board"], {})
        self.assertEqual(result["bios"], {})

    def test_windows_firmware_missing_fields_returns_empty_objects(self):
        with patch.object(system, "powershell_json", return_value=None):
            board, bios = system._windows_firmware()
        self.assertEqual(board, {})
        self.assertEqual(bios, {})


class SharedCollectorHelperTests(unittest.TestCase):
    def test_missing_powershell_is_a_supported_empty_result(self):
        with patch.object(common.shutil, "which", return_value=None):
            self.assertIsNone(common.powershell_json("Get-CimInstance test"))

    def test_failed_command_is_a_supported_empty_result(self):
        with (
            patch.object(common.shutil, "which", return_value="powershell"),
            patch.object(common.subprocess, "run", side_effect=subprocess.TimeoutExpired("powershell", 1)),
        ):
            self.assertIsNone(common.powershell_json("Get-CimInstance test"))


if __name__ == "__main__":
    unittest.main()