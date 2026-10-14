import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import agent_loop
from memory import ObservationMemory
from units import format_bytes, format_capacity_text, format_gib, public_units


class UnitDisplayTests(unittest.TestCase):
    def test_capacity_uses_mb_then_gb_without_scientific_notation(self):
        self.assertEqual(format_bytes(32768), "0.033 MB")
        self.assertEqual(format_bytes(1_000_000), "1 MB")
        self.assertEqual(format_bytes(1_000_000_000), "1 GB")
        self.assertEqual(format_gib(0.000031), "0.033 MB")
        self.assertEqual(format_capacity_text("约 3.1e-05 GiB"), "约 0.033 MB")

    def test_model_receives_formatted_capacity_and_answer_is_normalized(self):
        async def fake_check(_args, _inventory, _memory):
            return {"asset": "host", "memory": {"available_gib": 4.0},
                    "disks": [{"available_gib": 0.000031}]}

        replies = [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function",
                "function": {"name": "load_skill", "arguments": '{"name":"snmp-diagnosis"}'}}]},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_2", "type": "function",
                "function": {"name": "check_snmp", "arguments": '{"asset":"host"}'}}]},
            {"role": "assistant", "content": "磁盘剩余约 3.1e-05 GiB。"},
        ]
        with tempfile.TemporaryDirectory() as folder, \
             patch("app._chat_complete", side_effect=replies), \
             patch.dict("app.TOOL_HANDLERS", {"check_snmp": fake_check}):
            messages = [{"role": "user", "content": "磁盘还有多少空间？"}]
            answer = asyncio.run(agent_loop(messages, {"host": {}},
                                            ObservationMemory(Path(folder) / "memory.json")))
        tool_result = json.loads(messages[4]["content"])
        self.assertEqual(tool_result["memory"]["available_size"], "4.29 GB")
        self.assertEqual(tool_result["disks"][0]["available_size"], "0.033 MB")
        self.assertNotIn("available_gib", messages[4]["content"])
        self.assertEqual(answer, "磁盘剩余约 0.033 MB。")
        self.assertEqual(messages[-1]["content"], answer)

    def test_history_conversion_keeps_original_values_untouched(self):
        observation = {"total_gib": 0.000031, "nested": [{"memory_used_gib": None}]}
        converted = public_units(observation)
        self.assertEqual(converted["total_size"], "0.033 MB")
        self.assertEqual(converted["nested"][0]["memory_used_size"], "—")
        self.assertIn("total_gib", observation)


if __name__ == "__main__":
    unittest.main()
