import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import TOOL_HANDLERS, build_tools, run_check_docker
from inventory import load_inventory
from memory import ObservationMemory
from service_probes import _gib, _mysql_check, _mysql_catalog, run_service_probe


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.inventory = {"host": {"kind": "snmp_v2c", "host": "192.0.2.10", "services": {
            "docker": {"host_env": "TEST_HOST", "port_env": "TEST_PORT",
                       "user_env": "TEST_USER", "password_env": "TEST_PASSWORD"},
            "mysql": {"host_env": "TEST_HOST", "port_env": "TEST_PORT",
                      "user_env": "TEST_USER", "password_env": "TEST_PASSWORD"},
        }}}

    def test_tool_registry_and_units(self):
        self.assertEqual({item["function"]["name"] for item in build_tools()}, set(TOOL_HANDLERS))
        self.assertEqual(_gib("1024 MiB"), 1.0)
        self.assertEqual(_gib("1GB"), 0.931)

    def test_docker_tool_includes_asset_display_name(self):
        self.inventory["host"]["display_name"] = "Linux 主机"
        with tempfile.TemporaryDirectory() as folder, \
             patch("app.run_service_probe", return_value={"asset": "host", "kind": "docker", "ok": True}):
            memory = ObservationMemory(Path(folder) / "memory.json")
            result = asyncio.run(run_check_docker({"asset": "host"}, self.inventory, memory))
        self.assertEqual(result["display_name"], "Linux 主机")
        self.assertEqual(result["asset"], "host")

    def test_only_registered_service_and_matching_host_can_be_queried(self):
        with tempfile.TemporaryDirectory() as folder:
            memory = ObservationMemory(Path(folder) / "memory.json")
            with patch.dict(os.environ, {"TEST_HOST": "192.0.2.11", "TEST_PORT": "2223",
                                         "TEST_USER": "root", "TEST_PASSWORD": "test"}):
                result = asyncio.run(run_service_probe("host", "docker", self.inventory, memory))
            self.assertFalse(result["ok"])
            self.assertNotIn("test", json.dumps(result))
            self.assertEqual(memory.last("host"), result)
            unknown = asyncio.run(run_service_probe("192.0.2.11", "docker", self.inventory, memory))
            self.assertFalse(unknown["ok"])

    def test_mysql_check_returns_curated_values_without_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            memory = ObservationMemory(Path(folder) / "memory.json")
            with patch.dict(os.environ, {"TEST_HOST": "192.0.2.10", "TEST_PORT": "3308",
                                         "TEST_USER": "monitor", "TEST_PASSWORD": "secret"}), \
                 patch("service_probes._mysql_check", return_value={"version": "5.7.44", "status": {"Uptime": 123}}):
                result = asyncio.run(run_service_probe("192.0.2.10", "mysql", self.inventory, memory))
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"]["Uptime"], 123)
            self.assertNotIn("secret", memory.path.read_text(encoding="utf-8"))

    def test_mysql_check_discovers_databases_tables_and_views(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [("5.7.44", "example-mysql", 0),
                                       ("max_connections", "151")]
        cursor.fetchall.side_effect = [
            [("Uptime", "123"), ("Threads_connected", "3"),
             ("Innodb_buffer_pool_reads", "1"), ("Innodb_buffer_pool_read_requests", "100")],
            [("example_logs",), ("example_store",)],
            [("example_logs", 1, 0, 3, 16384, 16384),
             ("example_store", 2, 1, 7, 32768, 16384)],
            [("example_logs", "events", "InnoDB", 3, 16384, 16384),
             ("example_store", "customers", "InnoDB", 3, 16384, 0),
             ("example_store", "orders", "InnoDB", 4, 16384, 16384)],
            [("example_store", "order_summary")],
        ]
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        with patch("pymysql.connect", return_value=connection):
            result = _mysql_check("192.0.2.10", 3308, "monitor", "secret")
        self.assertEqual(result["endpoint"], "192.0.2.10:3308")
        self.assertEqual((result["database_count"], result["table_count"], result["view_count"]),
                         (2, 3, 1))
        self.assertEqual(result["databases"][1]["name"], "example_store")
        self.assertEqual(result["tables"][2]["name"], "orders")
        self.assertEqual(result["tables"][2]["estimated_rows"], 4)
        self.assertGreater(result["tables"][2]["total_gib"], 0)
        self.assertEqual(result["views"][0]["name"], "order_summary")
        self.assertFalse(result["tables_truncated"])
        self.assertNotIn("secret", json.dumps(result))
        connection.close.assert_called_once()

    def test_mysql_table_details_are_bounded_without_losing_total_count(self):
        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            [("shop",)],
            [("shop", 51, 0, 51, 51 * 16384, 0)],
            [("shop", f"table_{number}", "InnoDB", 1, 16384, 0) for number in range(51)],
            [],
        ]
        result = _mysql_catalog(cursor)
        self.assertEqual(result["table_count"], 51)
        self.assertEqual(len(result["tables"]), 50)
        self.assertTrue(result["tables_truncated"])
        self.assertFalse(result["views_truncated"])

    def test_invalid_service_configuration_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "inventory.json"
            path.write_text(json.dumps({"assets": {"bad": {"kind": "snmp_v2c", "host": "192.0.2.10",
                "community_env": "SNMP_COMMUNITY", "profile": "linux_host", "services": {"docker": {"password": "secret"}}}}}),
                encoding="utf-8")
            with self.assertRaises(ValueError):
                load_inventory(path)


if __name__ == "__main__":
    unittest.main()
