"""Agentic Monitor：模型选择只读工具，Python 按需查询。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from inventory import load_inventory, resolve_asset
from memory import ObservationMemory
from probes import run_probe
from service_probes import run_service_probe
from units import format_capacity_text, public_units


ROOT = Path(__file__).resolve().parent
SKILL_NAMES = ("snmp-diagnosis", "mysql-diagnosis")
REQUIRED_SKILLS = {"check_snmp": "snmp-diagnosis", "check_mysql": "mysql-diagnosis"}
SYSTEM_PROMPT = """你是按需诊断设备与服务的中文助手。你只能使用工具查询资产清单内的目标。
工具结果中的 asset/name 是内部标识；面向用户时优先使用 display_name，并可附上 IP。不要把内部标识当作主机显示名称。
用户问监控了哪些资产时，必须调用 list_assets，再根据工具结果回答。
可用技能：snmp-diagnosis（Linux 主机 SNMP 巡检）和 mysql-diagnosis（MySQL 人工接入与只读巡检）。只在问题涉及相应领域时调用 load_skill 读取对应技能。每个新问题都重新加载本次需要的技能；上一问题已加载的不算本次加载。读取技能后，再在下一轮调用对应的检查工具；不要在同一轮同时调用 load_skill 和该检查工具。
用户问主机当前情况时，先加载 snmp-diagnosis，再用已知资产名或 IP 调用 check_snmp；不清楚资产时先调用 list_assets。
每次询问当前状态都要重新查询，历史观测不能代替本次查询。
用户问 Docker 容器或服务时调用 check_docker。问 MySQL 当前状态、数据库或表时，先加载 mysql-diagnosis，再调用 check_mysql；只问账号接入方法时加载技能并说明人工步骤。问整台主机的综合情况时，可以查询主机、Docker 和 MySQL，并为 SNMP/MySQL 分别加载对应技能。
容量按十进制 MB/GB 展示，达到 1000 MB 时改用 GB。工具结果中的 *_size 已是格式化容量，不要再换算；回答中不要使用 GiB、MiB 或科学计数法表示容量。不同工具的 checked_at 可能不同。
监控回答区分实测值、你的判断、未能取得的指标，给出 checked_at。查询失败不能直接断言主机宕机。
监控回答用清楚的中文段落、适当的小标题、表格或列表组织：先呈现实测数据，再说明基于数据的判断；只有存在缺失指标、采样边界或其他注意事项时才补充说明。标题随问题内容自然选择，不强制固定格式，也不要为了凑小节重复信息。资产清单类简单问题可直接回答。
不自行执行系统命令或数据库写操作，不修改设备，不访问清单之外的地址。用户询问接入方法时，可以给出由管理员审阅并人工执行的配置步骤。"""


# 显式列出模型能看见的只读工具。
# input_schema 是 JSON Schema；这里只提供只读诊断工具。
TOOLS = [
    {
        "name": "list_assets",
        "description": "列出已登记资产的名称、类型、IP、服务和说明。问监控了哪些资产时必须调用。",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "name": "load_skill",
        "description": "按需读取已登记的 SNMP 或 MySQL 诊断流程。需先读取技能，再在下一轮执行对应检查。",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "enum": list(SKILL_NAMES),
                                     "description": "要读取的技能名称"}},
            "required": ["name"], "additionalProperties": False,
        },
    },
    {
        "name": "check_snmp",
        "description": "实时查询已登记 SNMP 主机的 CPU、内存、分区、网卡。只接受登记资产名或登记 IP，不接受任意目标或 OID。",
        "input_schema": {
            "type": "object",
            "properties": {"asset": {"type": "string", "description": "inventory.json 中的资产名或登记的 IP"}},
            "required": ["asset"],
            "additionalProperties": False,
        },
    },
    {
        "name": "check_docker",
        "description": "通过 SSH 只读查询已登记主机的 Docker 版本、容器状态及运行容器资源使用。",
        "input_schema": {
            "type": "object",
            "properties": {"asset": {"type": "string", "description": "inventory.json 中的资产名或登记的 IP"}},
            "required": ["asset"], "additionalProperties": False,
        },
    },
    {
        "name": "check_mysql",
        "description": "直连已登记主机的 MySQL，读取实例状态、连接数、InnoDB 指标，以及可见业务库、表、视图、估算行数和空间用量。",
        "input_schema": {
            "type": "object",
            "properties": {"asset": {"type": "string", "description": "inventory.json 中的资产名或登记的 IP"}},
            "required": ["asset"], "additionalProperties": False,
        },
    },
    {
        "name": "observation_history",
        "description": "读取资产最近五次已保存的观测结果，用于历史比较；不能代替当前查询。",
        "input_schema": {
            "type": "object",
            "properties": {"asset": {"type": "string", "description": "inventory.json 中的资产名或登记的 IP"}},
            "required": ["asset"],
            "additionalProperties": False,
        },
    },
]


async def run_list_assets(args: dict, inventory: dict[str, dict], memory: ObservationMemory) -> dict:
    return {
        "assets": [
            {"name": name, "display_name": config.get("display_name", name),
             "kind": config["kind"], "host": config.get("host"),
             "description": config.get("description", ""),
             "services": list(config.get("services", {}))}
            for name, config in inventory.items()
        ]
    }


async def run_load_skill(args: dict, inventory: dict[str, dict], memory: ObservationMemory) -> dict:
    name = args["name"]
    if name not in SKILL_NAMES:
        raise ValueError("未知技能")
    skill_path = ROOT / ".claude" / "skills" / name / "SKILL.md"
    return {"name": name, "instructions": skill_path.read_text(encoding="utf-8")}


def _with_display_name(result: dict, inventory: dict[str, dict]) -> dict:
    asset = result.get("asset")
    if asset in inventory:
        return {**result, "display_name": inventory[asset].get("display_name", asset)}
    return result


def display_asset_names(answer: str, inventory: dict[str, dict]) -> str:
    """Use public asset names in assistant prose, including model-echoed IDs."""
    for asset, config in sorted(inventory.items(), key=lambda item: len(item[0]), reverse=True):
        display_name = config.get("display_name")
        if display_name and display_name != asset:
            answer = answer.replace(asset, display_name)
    return answer


async def run_check_snmp(args: dict, inventory: dict[str, dict], memory: ObservationMemory) -> dict:
    return _with_display_name(await run_probe(args["asset"], inventory, memory), inventory)


async def run_check_docker(args: dict, inventory: dict[str, dict], memory: ObservationMemory) -> dict:
    return _with_display_name(await run_service_probe(args["asset"], "docker", inventory, memory), inventory)


async def run_check_mysql(args: dict, inventory: dict[str, dict], memory: ObservationMemory) -> dict:
    return _with_display_name(await run_service_probe(args["asset"], "mysql", inventory, memory), inventory)


async def run_observation_history(args: dict, inventory: dict[str, dict], memory: ObservationMemory) -> dict:
    try:
        asset = resolve_asset(args["asset"], inventory)
    except ValueError as exc:
        return {"asset": args["asset"], "error": str(exc)}
    return {"asset": asset, "display_name": inventory[asset].get("display_name", asset),
            "historical": True, "observations": memory.observation_history(asset)[-5:]}


# 名字必须与 TOOLS 中的 name 完全一致。这里在启动时校验，避免运行到某次调用才发现写错。
TOOL_HANDLERS = {
    "list_assets": run_list_assets,
    "load_skill": run_load_skill,
    "check_snmp": run_check_snmp,
    "check_docker": run_check_docker,
    "check_mysql": run_check_mysql,
    "observation_history": run_observation_history,
}


def validate_tool_registry() -> None:
    names = [definition["name"] for definition in TOOLS]
    if len(names) != len(set(names)) or set(names) != set(TOOL_HANDLERS):
        raise ValueError("TOOLS 与 TOOL_HANDLERS 的工具名称不一致，或有重复名称")


def build_tools() -> list[dict]:
    """把本地 JSON Schema 转成 OpenAI 兼容的 function tools。"""
    validate_tool_registry()
    return [{"type": "function", "function": {
        "name": item["name"], "description": item["description"], "parameters": item["input_schema"]
    }} for item in TOOLS]


def _chat_complete(messages: list[dict], tools: list[dict]) -> dict:
    key = os.getenv("MODEL_API_KEY")
    base_url = os.getenv("MODEL_BASE_URL", "")
    model = os.getenv("MODEL_NAME", "")
    if not key or not base_url or not model:
        raise ValueError("请在 .env 中配置 MODEL_API_KEY、MODEL_BASE_URL 和 MODEL_NAME")
    if not base_url.startswith("https://"):
        raise ValueError("MODEL_BASE_URL 必须使用 HTTPS")
    payload = json.dumps({"model": model,
                          "messages": messages, "tools": tools, "tool_choice": "auto",
                          "stream": False}, ensure_ascii=False).encode("utf-8")
    request = Request(base_url.rstrip("/") + "/chat/completions", data=payload, method="POST",
                      headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=120) as response:
            body = json.load(response)
    except HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", errors="replace").replace(key, "[redacted]")
        raise RuntimeError(f"模型 API 返回 HTTP {exc.code}: {detail}") from None
    except URLError as exc:
        raise RuntimeError(f"模型 API 连接失败: {exc.reason}") from None
    try:
        return body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("模型 API 返回格式不正确") from exc


async def agent_loop(messages: list[dict], inventory: dict[str, dict], memory: ObservationMemory) -> str:
    """模型按需读取技能并选择只读工具，最多六轮。"""
    tools = build_tools()
    loaded_skills: set[str] = set()
    for _ in range(6):
        available_tools = [tool for tool in tools
                           if tool["function"]["name"] not in REQUIRED_SKILLS
                           or REQUIRED_SKILLS[tool["function"]["name"]] in loaded_skills]
        message = await asyncio.to_thread(_chat_complete, messages, available_tools)
        calls = message.get("tool_calls") or []
        if not calls:
            answer = message.get("content")
            if not isinstance(answer, str) or not answer.strip():
                raise RuntimeError("模型没有返回可显示的回答")
            answer = format_capacity_text(display_asset_names(answer, inventory))
            messages.append({"role": "assistant", "content": answer})
            return answer
        messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": calls})
        ready_skills = loaded_skills.copy()
        for call in calls:
            name = call.get("function", {}).get("name")
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
                if not isinstance(args, dict):
                    raise ValueError("工具参数必须是对象")
                handler = TOOL_HANDLERS.get(name)
                if handler is None:
                    raise ValueError(f"未知工具: {name}")
                required = REQUIRED_SKILLS.get(name)
                if required and required not in ready_skills:
                    raise ValueError(f"请先调用 load_skill 读取 {required}，下一轮再调用 {name}")
                value = await handler(args, inventory, memory)
                if name == "load_skill":
                    loaded_skills.add(value["name"])
            except (KeyError, ValueError, OSError) as exc:
                value = {"error": f"{type(exc).__name__}: {exc}"}
            detail = f" {value['name']}" if name == "load_skill" and "name" in value else ""
            print(f"[tool] {name}{detail}", file=sys.stderr)
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(value if name == "load_skill" else public_units(value),
                                                   ensure_ascii=False)})
    raise RuntimeError("模型连续调用工具超过 6 轮")


async def ask(question: str, inventory: dict[str, dict], memory: ObservationMemory,
              messages: list[dict] | None = None) -> str:
    if messages is None:
        messages = []
    if not messages:
        messages.append({"role": "system", "content": SYSTEM_PROMPT})
    elif messages[0].get("role") == "system":
        # Keep an open web/terminal conversation aligned with the current instructions.
        messages[0]["content"] = SYSTEM_PROMPT
    messages.append({"role": "user", "content": question})
    return await agent_loop(messages, inventory, memory)


async def main() -> int:
    parser = argparse.ArgumentParser(description="按需查询已登记资产的 SNMP、Docker 和 MySQL 状态")
    parser.add_argument("question", nargs="*", help="Ask one question; omit to start a REPL")
    parser.add_argument("--inventory", type=Path, default=ROOT / "inventory.json")
    parser.add_argument("--memory", type=Path, default=ROOT / ".data" / "observations.json")
    parser.add_argument("--list", action="store_true", help="List configured assets without calling a model")
    parser.add_argument("--probe", metavar="ASSET", help="Run one probe without calling a model")
    parser.add_argument("--service", choices=("snmp", "docker", "mysql"),
                        help="Select a probe with --probe (default: SNMP)")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    inventory = load_inventory(args.inventory)
    memory = ObservationMemory(args.memory)

    if args.list:
        for name, config in inventory.items():
            services = ",".join(config.get("services", {}))
            print(f"{config.get('display_name', name)}\t{config['kind']}\t{services}\t{config.get('description', '')}")
        return 0
    if args.probe:
        if args.service in ("docker", "mysql"):
            result = await run_service_probe(args.probe, args.service, inventory, memory)
        else:
            result = await run_probe(args.probe, inventory, memory)
        print(json.dumps(public_units(result), ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    if args.service:
        parser.error("--service requires --probe")

    if not os.getenv("MODEL_API_KEY"):
        raise ValueError("MODEL_API_KEY 未设置；请在环境变量或 .env 中配置")

    if args.question:
        print(await ask(" ".join(args.question), inventory, memory))
        return 0

    print("Agentic Monitor：对话式监控智能体。输入问题，输入 q 退出。")
    messages: list[dict] = []
    while True:
        try:
            question = input("agentic-monitor> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if question.lower() in ("q", "quit", "exit"):
            return 0
        if question:
            try:
                print(await ask(question, inventory, memory, messages))
            except Exception as exc:
                print(f"Error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
