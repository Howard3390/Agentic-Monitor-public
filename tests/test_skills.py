import asyncio
import json
import unittest
from unittest.mock import patch

from app import ask, run_load_skill


def tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}


class SkillFlowTests(unittest.TestCase):
    def test_mysql_skill_loads_before_mysql_probe(self):
        observed = []

        async def fake_check(_args, _inventory, _memory):
            observed.append("checked")
            return {"asset": "host", "ok": True, "checked_at": "2026-10-11T00:00:00+00:00"}

        replies = iter([
            {"role": "assistant", "content": None, "tool_calls": [
                tool_call("1", "load_skill", {"name": "mysql-diagnosis"})]},
            {"role": "assistant", "content": None, "tool_calls": [
                tool_call("2", "check_mysql", {"asset": "host"})]},
            {"role": "assistant", "content": "MySQL 查询完成。"},
        ])
        first_prompt = []
        offered_tools = []

        def fake_chat(messages, tools):
            if not first_prompt:
                first_prompt.append(messages[0]["content"])
            offered_tools.append({item["function"]["name"] for item in tools})
            return next(replies)

        messages = []
        with patch("app._chat_complete", side_effect=fake_chat), \
             patch.dict("app.TOOL_HANDLERS", {"check_mysql": fake_check}):
            answer = asyncio.run(ask("查看 MySQL 状态", {"host": {}}, None, messages))

        self.assertEqual(answer, "MySQL 查询完成。")
        self.assertEqual(observed, ["checked"])
        self.assertIn("mysql-diagnosis", first_prompt[0])
        self.assertNotIn("## 日常巡检", first_prompt[0])
        self.assertNotIn("check_mysql", offered_tools[0])
        self.assertIn("check_mysql", offered_tools[1])
        skill_result = json.loads(messages[3]["content"])
        self.assertEqual(skill_result["name"], "mysql-diagnosis")
        self.assertIn("## 日常巡检", skill_result["instructions"])

    def test_mysql_probe_waits_for_skill_in_separate_model_round(self):
        observed = []

        async def fake_check(_args, _inventory, _memory):
            observed.append("checked")
            return {"ok": True}

        replies = iter([
            {"role": "assistant", "content": None, "tool_calls": [
                tool_call("1", "load_skill", {"name": "mysql-diagnosis"}),
                tool_call("2", "check_mysql", {"asset": "host"})]},
            {"role": "assistant", "content": "需要下一轮查询。"},
        ])
        with patch("app._chat_complete", side_effect=lambda _messages, _tools: next(replies)), \
             patch.dict("app.TOOL_HANDLERS", {"check_mysql": fake_check}):
            messages = []
            asyncio.run(ask("查看 MySQL 状态", {"host": {}}, None, messages))

        self.assertEqual(observed, [])
        error = json.loads(messages[4]["content"])
        self.assertIn("请先调用 load_skill", error["error"])

    def test_load_skill_rejects_unknown_name(self):
        with self.assertRaisesRegex(ValueError, "未知技能"):
            asyncio.run(run_load_skill({"name": "../../.env"}, {}, None))


if __name__ == "__main__":
    unittest.main()
