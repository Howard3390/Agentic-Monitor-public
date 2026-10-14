import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from inventory import load_inventory
from memory import ObservationMemory
from probes import run_probe


class BasicTests(unittest.TestCase):
    def test_persistent_observation_memory(self):
        with tempfile.TemporaryDirectory() as folder:
            memory = ObservationMemory(Path(folder) / "observations.json")
            result = {"asset": "host", "kind": "snmp_v2c", "checked_at": "2026-01-01T00:00:00+00:00",
                      "ok": True, "cpu": {"utilization_percent": 5}}
            memory.record(result)
            self.assertEqual(ObservationMemory(memory.path).last("host"), result)
            self.assertEqual(len(memory.observation_history("host")), 1)

    def test_unknown_asset_cannot_be_probed(self):
        with tempfile.TemporaryDirectory() as folder:
            memory = ObservationMemory(Path(folder) / "observations.json")
            result = asyncio.run(run_probe("192.0.2.99", {}, memory))
            self.assertFalse(result["ok"])
            self.assertFalse(memory.path.exists())

    def test_inventory_rejects_removed_http_asset_kind(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "inventory.json"
            path.write_text(json.dumps({"assets": {"web": {"kind": "http", "url": "https://example.com/"}}}),
                            encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported kind"):
                load_inventory(path)


if __name__ == "__main__":
    unittest.main()
