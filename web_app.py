"""Streamlit chat and on-demand observation dashboard for the existing Agent."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import os

import streamlit as st
from dotenv import load_dotenv

from app import ROOT, ask, display_asset_names
from inventory import load_inventory
from memory import ObservationMemory
from units import format_capacity_text, format_gib


def _latest_by_kind(history: list[dict]) -> dict[str, dict]:
    latest = {}
    for entry in history:
        if isinstance(entry, dict) and entry.get("kind") in ("snmp_v2c", "docker", "mysql"):
            latest[entry["kind"]] = entry
    return latest


def _value(number: object, unit: str = "") -> str:
    if number is None:
        return "—"
    return f"{number}{unit}"


def _observed_at(observation: dict) -> None:
    st.caption(f"上次查询：{observation.get('checked_at', '未知')}（UTC）")
    if not observation.get("ok"):
        st.error(f"上次查询失败：{observation.get('error', '原因未知')}")


def _show_snmp(observation: dict) -> None:
    st.subheader("主机 · SNMP")
    _observed_at(observation)
    if not observation.get("ok"):
        return
    cpu = observation.get("cpu") or {}
    memory = observation.get("memory") or {}
    first, second, third = st.columns(3)
    first.metric("CPU 使用率", _value(cpu.get("utilization_percent"), "%"))
    second.metric("可用内存", format_gib(memory.get("available_gib")))
    third.metric("总内存", format_gib(memory.get("total_gib")))

    disks = [{"分区": row.get("path"), "剩余容量": format_gib(row.get("available_gib")),
              "使用率 (%)": row.get("used_percent"), "inode 使用率 (%)": row.get("inode_used_percent")}
             for row in observation.get("disks", [])]
    if disks:
        st.markdown("**分区**")
        st.dataframe(disks, hide_index=True, width="stretch")

    interfaces = [{"网卡": row.get("name"), "在线": row.get("up"),
                   "接收 (B/s)": row.get("rx_bytes_per_second"),
                   "发送 (B/s)": row.get("tx_bytes_per_second")}
                  for row in observation.get("interfaces", [])]
    if interfaces:
        st.markdown("**网卡**")
        st.dataframe(interfaces, hide_index=True, width="stretch")

    for warning in observation.get("warnings", []):
        st.warning(warning)


def _show_docker(observation: dict) -> None:
    st.subheader("Docker")
    _observed_at(observation)
    if not observation.get("ok"):
        return
    first, second, third = st.columns(3)
    first.metric("Docker 版本", _value(observation.get("docker_version")))
    second.metric("容器总数", _value(observation.get("container_count")))
    third.metric("运行中", _value(observation.get("running_count")))
    containers = [{"容器": row.get("name"), "镜像": row.get("image"),
                   "状态": row.get("status") or row.get("state"), "CPU": row.get("cpu_percent"),
                   "内存": format_gib(row.get("memory_used_gib")),
                   "内存上限": format_gib(row.get("memory_limit_gib"))}
                  for row in observation.get("containers", [])]
    if containers:
        st.dataframe(containers, hide_index=True, width="stretch")
    else:
        st.info("本次查询没有发现容器。")
    if observation.get("truncated"):
        st.caption("容器列表只展示前 50 个。")


def _show_mysql(observation: dict) -> None:
    st.subheader("MySQL")
    _observed_at(observation)
    if not observation.get("ok"):
        return
    status = observation.get("status") or {}
    first, second, third = st.columns(3)
    first.metric("版本", _value(observation.get("version")))
    second.metric("当前连接", _value(status.get("Threads_connected")))
    third.metric("连接使用率", _value(observation.get("connection_usage_percent"), "%"))
    st.dataframe([{"运行时间 (秒)": status.get("Uptime"),
                   "运行中线程": status.get("Threads_running"),
                   "累计请求": status.get("Questions"),
                   "累计慢查询": status.get("Slow_queries"),
                   "InnoDB 缓冲数据": format_gib(observation.get("innodb_buffer_pool_data_gib")),
                   "缓冲池命中率 (%)": observation.get("innodb_buffer_pool_hit_percent")}],
                 hide_index=True, width="stretch")
    st.caption("请求数和慢查询数为 MySQL 启动后的累计值。")

    if observation.get("metadata_error"):
        st.warning("数据库与表信息查询失败；实例状态仍可用。错误类型：" + observation["metadata_error"])
    elif "database_count" in observation:
        st.markdown("**业务库与表**")
        database_col, table_col, view_col = st.columns(3)
        database_col.metric("业务库", observation["database_count"])
        table_col.metric("表", observation["table_count"])
        view_col.metric("视图", observation["view_count"])
        if not observation["databases"]:
            st.info("当前监控账号未发现可见的业务数据库。")
        else:
            st.dataframe([
                {"数据库": row["name"], "表数": row["table_count"], "视图数": row["view_count"],
                 "估算行数": row["estimated_rows"], "数据": format_gib(row["data_gib"]),
                 "索引": format_gib(row["index_gib"]), "合计": format_gib(row["total_gib"])}
                for row in observation["databases"]
            ], hide_index=True, width="stretch")
        if observation["tables"]:
            st.markdown("**表明细**")
            st.dataframe([
                {"数据库": row["database"], "表": row["name"], "引擎": row["engine"],
                 "估算行数": row["estimated_rows"], "数据": format_gib(row["data_gib"]),
                 "索引": format_gib(row["index_gib"]), "合计": format_gib(row["total_gib"])}
                for row in observation["tables"]
            ], hide_index=True, width="stretch")
        if observation["views"]:
            st.markdown("**视图**")
            st.dataframe([{"数据库": row["database"], "视图": row["name"]}
                          for row in observation["views"]], hide_index=True, width="stretch")
        if observation["tables_truncated"] or observation["views_truncated"]:
            st.warning("表或视图明细仅展示前 50 项；上方数量为完整统计。")
        st.caption("InnoDB 行数及库表空间为估算值；容量按 MB 或 GB 显示。")


def main() -> None:
    st.set_page_config(page_title="Agentic Monitor", page_icon="🖥️", layout="wide")
    load_dotenv(ROOT / ".env")
    try:
        inventory = load_inventory(ROOT / "inventory.json")
    except ValueError as exc:
        st.error(f"资产清单读取失败：{exc}")
        st.stop()
    memory = ObservationMemory(ROOT / ".data" / "observations.json")

    if "model_messages" not in st.session_state:
        st.session_state.model_messages = []
    if "visible_messages" not in st.session_state:
        st.session_state.visible_messages = []

    with st.sidebar:
        st.title("Agentic Monitor")
        selected = st.selectbox("查看资产", list(inventory), format_func=lambda name: f"{inventory[name].get('display_name', name)} · {inventory[name].get('host', '')}")
        services = ", ".join(inventory[selected].get("services", {}))
        st.caption(f"可查询：SNMP{', ' + services if services else ''}")
        if st.button("开始新对话", width="stretch"):
            st.session_state.model_messages = []
            st.session_state.visible_messages = []
        st.caption("网页只在提问时采集；仪表板显示已保存的最近一次结果。")

    st.title("Agentic Monitor")
    st.subheader("对话式监控智能体")
    st.caption("按需查询系统与服务的运行状态")
    chat_tab, dashboard_tab = st.tabs(["对话", "监控数据"])

    with chat_tab:
        st.caption("例如：这台主机上的 Docker 和 MySQL 现在怎么样？")
        for message in st.session_state.visible_messages:
            with st.chat_message(message["role"]):
                content = (format_capacity_text(display_asset_names(message["content"], inventory))
                           if message["role"] == "assistant" else message["content"])
                st.markdown(content)
                if message.get("tools"):
                    st.caption("调用工具：" + "、".join(message["tools"]))

        # Inside a tab, st.chat_input renders inline; st.bottom keeps it below every reply.
        if prompt := st.bottom.chat_input("询问监控状态，或与智能体讨论你关心的问题"):
            st.session_state.visible_messages.append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.write(prompt)
            with st.chat_message("assistant"):
                if not os.getenv("MODEL_API_KEY"):
                    error = "MODEL_API_KEY 未设置；请检查项目根目录的 .env。"
                    st.error(error)
                    st.session_state.visible_messages.append({"role": "assistant", "content": error})
                else:
                    prior = st.session_state.model_messages
                    messages = deepcopy(prior)
                    try:
                        with st.spinner("正在查询资产并生成回答…"):
                            answer = asyncio.run(ask(prompt, inventory, memory, messages))
                    except Exception as exc:
                        error = f"查询失败：{type(exc).__name__}: {exc}"
                        st.error(error)
                        st.session_state.visible_messages.append({"role": "assistant", "content": error})
                    else:
                        tools = [call["function"]["name"] for message in messages[len(prior):]
                                 for call in message.get("tool_calls", [])]
                        st.session_state.model_messages = messages
                        st.session_state.visible_messages.append(
                            {"role": "assistant", "content": answer, "tools": tools})
                        st.markdown(answer)
                        if tools:
                            st.caption("调用工具：" + "、".join(tools))

    with dashboard_tab:
        st.caption("以下是历史记录中各项检查的最近一次结果（UTC），打开页面不会自动重新采集。")
        try:
            history = memory.observation_history(selected)
        except ValueError as exc:
            st.error(f"观测记录读取失败：{exc}")
            return
        latest = _latest_by_kind(history)
        if not latest:
            st.info("暂无观测记录。在对话中询问资产情况后，这里会显示采集结果。")
        for kind, renderer in (("snmp_v2c", _show_snmp), ("docker", _show_docker), ("mysql", _show_mysql)):
            if kind not in latest:
                continue
            renderer(latest[kind])
            st.divider()


if __name__ == "__main__":
    main()
