import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import agent_loop
from inventory import resolve_asset
from memory import ObservationMemory
from snmp_host import (
    CPU_IDLE, DISK_AVAIL, DISK_INODE_PERCENT, DISK_PATH, DISK_USED_PERCENT,
    HR_DESCR, HR_SIZE, HR_UNITS, HR_USED, IF_IN, IF_NAME, IF_OUT, IF_STATUS,
    MEM_AVAILABLE, MEM_FREE, MEM_TOTAL, _SnmpReader, inspect_linux_host,
)


class _Engine:
    def close_dispatcher(self):
        pass


class _Reader:
    def __init__(self, *_):
        self.network_calls = 0

    async def walk(self, prefix, limit=32):
        return {3: "/"} if prefix == DISK_PATH else {2: "eth0"} if prefix == IF_NAME else {}

    async def get(self, oids):
        if oids == [CPU_IDLE, MEM_TOTAL, MEM_FREE, MEM_AVAILABLE]:
            return {CPU_IDLE: 99, MEM_TOTAL: 4 * 1024 * 1024,
                    MEM_FREE: 1024 * 1024, MEM_AVAILABLE: 2 * 1024 * 1024}
        if oids[0].startswith(DISK_AVAIL):
            return {f"{DISK_AVAIL}.3": 2 * 1024 * 1024, f"{DISK_USED_PERCENT}.3": 50,
                    f"{DISK_INODE_PERCENT}.3": 10}
        self.network_calls += 1
        return {f"{IF_STATUS}.2": 1, f"{IF_IN}.2": 1000 + self.network_calls * 100,
                f"{IF_OUT}.2": 2000 + self.network_calls * 200}


class _FallbackReader(_Reader):
    async def walk(self, prefix, limit=32):
        if prefix == DISK_PATH:
            return {}
        if prefix == HR_DESCR:
            return {11: "Available memory", 34: "/"}
        return {}

    async def get(self, oids):
        if oids[0] == f"{HR_UNITS}.34":
            return {f"{HR_UNITS}.34": 4096, f"{HR_SIZE}.34": 262144, f"{HR_USED}.34": 65536}
        return await super().get(oids)


class SnmpHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_linux_profile_discovers_rows_and_calculates_values(self):
        with patch("snmp_host.SnmpEngine", return_value=_Engine()), \
             patch("snmp_host.UdpTransportTarget.create", new_callable=AsyncMock, return_value=object()), \
             patch("snmp_host._SnmpReader", _Reader):
            result = await inspect_linux_host("192.0.2.20", 161, "test-only")
        self.assertTrue(result["ok"])
        self.assertEqual(result["cpu"]["utilization_percent"], 1)
        self.assertEqual(result["memory"]["total_gib"], 4.0)
        self.assertEqual(result["memory"]["free_gib"], 1.0)
        self.assertEqual(result["memory"]["available_gib"], 2.0)
        self.assertEqual(result["memory"]["free_percent"], 25.0)
        self.assertEqual(result["memory"]["available_percent"], 50.0)
        self.assertNotIn("total_kb", result["memory"])
        self.assertEqual(result["disks"][0]["path"], "/")
        self.assertEqual(result["disks"][0]["available_gib"], 2.0)
        self.assertNotIn("available_kb", result["disks"][0])
        self.assertEqual(result["interfaces"][0]["name"], "eth0")
        self.assertIn("rx_bytes_per_second", result["interfaces"][0], result)

    async def test_storage_falls_back_to_host_resources_mib(self):
        with patch("snmp_host.SnmpEngine", return_value=_Engine()), \
             patch("snmp_host.UdpTransportTarget.create", new_callable=AsyncMock, return_value=object()), \
             patch("snmp_host._SnmpReader", _FallbackReader):
            result = await inspect_linux_host("192.0.2.20", 161, "test-only")
        self.assertTrue(result["ok"])
        self.assertEqual(result["disks"][0]["available_gib"], 0.75)
        self.assertEqual(result["disks"][0]["used_percent"], 25.0)
        self.assertIsNone(result["disks"][0]["inode_used_percent"])

    async def test_walk_requests_numeric_oids(self):
        from pysnmp.proto.rfc1902 import ObjectName
        from pysnmp.proto.rfc1902 import OctetString

        async def fake_walk(*args, **kwargs):
            self.assertFalse(kwargs["lookupMib"])
            yield (None, 0, 0, [(ObjectName(f"{IF_NAME}.2"), OctetString("eth0"))])

        with patch("snmp_host.walk_cmd", fake_walk):
            rows = await _SnmpReader(None, "public", None).walk(IF_NAME)
        self.assertEqual(rows, {2: "eth0"})

    def test_only_inventory_ip_is_allowed(self):
        inventory = {"example-linux": {"kind": "snmp_v2c", "host": "192.0.2.20"}}
        self.assertEqual(resolve_asset("192.0.2.20", inventory), "example-linux")
        with self.assertRaises(ValueError):
            resolve_asset("192.0.2.21", inventory)

    async def test_agent_calls_list_assets_and_returns_answer(self):
        inventory = {"example-linux": {"kind": "snmp_v2c", "host": "192.0.2.20",
                                  "display_name": "Linux 主机"}}
        replies = [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function",
                "function": {"name": "list_assets", "arguments": "{}"}}]},
            {"role": "assistant", "content": "目前监控 example-linux（192.0.2.20）。"},
        ]
        with tempfile.TemporaryDirectory() as folder, patch("app._chat_complete", side_effect=replies):
            messages = [{"role": "user", "content": "监控了哪些资产？"}]
            answer = await agent_loop(messages, inventory, ObservationMemory(Path(folder) / "memory.json"))
        self.assertIn("192.0.2.20", answer)
        self.assertIn("Linux 主机", answer)
        self.assertNotIn("example-linux", answer)
        self.assertEqual(messages[-1]["content"], answer)
        self.assertIn('"display_name": "Linux 主机"', messages[2]["content"])


if __name__ == "__main__":
    unittest.main()
