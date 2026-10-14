import os
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
from app import SYSTEM_PROMPT
from inventory import load_inventory


class WebAppTests(unittest.TestCase):
    def setUp(self):
        sample = load_inventory(Path(__file__).resolve().parents[1] / "inventory.example.json")
        patcher = patch("inventory.load_inventory", return_value=sample)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mysql_catalog_appears_in_saved_observation_view(self):
        page = Path(__file__).resolve().parents[1] / "web_app.py"
        observation = {
            "asset": "example-linux", "kind": "mysql", "ok": True,
            "checked_at": "2026-10-08T02:00:00+00:00", "version": "5.7.44",
            "status": {"Uptime": 100, "Threads_connected": 2},
            "database_count": 1, "table_count": 1, "view_count": 1,
            "databases": [{"name": "example_store", "table_count": 1, "view_count": 1,
                           "estimated_rows": 3, "data_gib": 0.000015,
                           "index_gib": 0.0, "total_gib": 0.000015}],
            "tables": [{"database": "example_store", "name": "orders", "engine": "InnoDB",
                        "estimated_rows": 3, "data_gib": 0.000015,
                        "index_gib": 0.0, "total_gib": 0.000015}],
            "views": [{"database": "example_store", "name": "order_summary"}],
            "tables_truncated": False, "views_truncated": False,
        }
        with patch("memory.ObservationMemory.observation_history", return_value=[observation]):
            page_test = AppTest.from_file(str(page)).run(timeout=30)
        self.assertFalse(page_test.exception)
        self.assertTrue(any(metric.label == "业务库" and str(metric.value) == "1"
                            for metric in page_test.metric))
        self.assertTrue(any("example_store" in str(frame.value) for frame in page_test.dataframe))
        self.assertTrue(any("0.016 MB" in str(frame.value) for frame in page_test.dataframe))
        self.assertFalse(any("GiB" in str(frame.value) for frame in page_test.dataframe))

    def test_existing_answer_displays_public_asset_name(self):
        page = Path(__file__).resolve().parents[1] / "web_app.py"
        page_test = AppTest.from_file(str(page)).run(timeout=30)
        page_test.session_state["visible_messages"] = [
            {"role": "assistant", "content": "资产：example-linux（192.0.2.20），空间约 3.1e-05 GiB"}
        ]
        page_test.run(timeout=30)
        self.assertFalse(page_test.exception)
        self.assertTrue(any("Linux 主机" in item.value for item in page_test.markdown))
        self.assertTrue(any("0.033 MB" in item.value for item in page_test.markdown))

    def test_chat_reuses_agent_and_shows_called_tools(self):
        page = Path(__file__).resolve().parents[1] / "web_app.py"
        replies = [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function",
                "function": {"name": "list_assets", "arguments": "{}"}}]},
            {"role": "assistant", "content": "已登记测试资产。"},
        ]
        with patch.dict(os.environ, {"MODEL_API_KEY": "test-only"}), \
             patch("app._chat_complete", side_effect=replies):
            page_test = AppTest.from_file(str(page)).run(timeout=30)
            self.assertFalse(page_test.exception)
            self.assertTrue(any(title.value == "Agentic Monitor" for title in page_test.title))
            self.assertTrue(any(heading.value == "对话式监控智能体" for heading in page_test.subheader))
            self.assertIn("Linux 主机", page_test.selectbox[0].options[0])
            page_test.chat_input[0].set_value("目前监控了哪些资产？").run(timeout=30)

        self.assertFalse(page_test.exception)
        visible = page_test.session_state["visible_messages"]
        self.assertEqual(visible[1]["content"], "已登记测试资产。")
        self.assertEqual(visible[1]["tools"], ["list_assets"])
        self.assertEqual(len(page_test.session_state["model_messages"]), 5)

    def test_snmp_answer_keeps_text_without_chart(self):
        page = Path(__file__).resolve().parents[1] / "web_app.py"
        observation = {"asset": "example-linux", "kind": "snmp_v2c", "ok": True,
                       "checked_at": "2026-10-07T15:02:52+00:00",
                       "cpu": {"utilization_percent": 5},
                       "memory": {"available_gib": 12.5},
                       "disks": [{"path": "/", "available_gib": 945.767, "used_percent": 6.1},
                                 {"path": "/var/lib/docker", "available_gib": 945.767, "used_percent": 6.1}]}

        async def fake_check(_args, _inventory, _memory):
            return observation

        replies = [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function",
                "function": {"name": "load_skill", "arguments": '{"name":"snmp-diagnosis"}'}}]},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_2", "type": "function",
                "function": {"name": "check_snmp", "arguments": '{"asset":"example-linux"}'}}]},
            {"role": "assistant", "content": "example-linux 磁盘使用率为 6.1%。\n\n**我的判断：**空间充足。\n\n**注意事项：**仅为本次采样。"},
        ]
        with patch.dict(os.environ, {"MODEL_API_KEY": "test-only"}), \
             patch("app._chat_complete", side_effect=replies), \
             patch.dict("app.TOOL_HANDLERS", {"check_snmp": fake_check}):
            page_test = AppTest.from_file(str(page)).run(timeout=30)
            page_test.chat_input[0].set_value("磁盘情况怎么样？").run(timeout=30)

        self.assertFalse(page_test.exception)
        answer = page_test.session_state["visible_messages"][1]
        self.assertIn("Linux 主机", answer["content"])
        self.assertNotIn("example-linux", answer["content"])
        self.assertIn("我的判断", answer["content"])
        self.assertIn("注意事项", answer["content"])
        self.assertNotIn("observations", answer)
        self.assertIn("不强制固定格式", SYSTEM_PROMPT)
        self.assertEqual(len(page_test.get("vega_lite_chart")), 0)


if __name__ == "__main__":
    unittest.main()
