"""通过 Net-SNMP 的固定 OID 查询 Linux 主机；模型不能指定 OID。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData, ContextData, ObjectIdentity, ObjectType, SnmpEngine,
    UdpTransportTarget, get_cmd, walk_cmd,
)


BASE = "1.3.6.1.4.1.2021"
KIB_PER_GIB = 1024 * 1024
BYTES_PER_GIB = 1024 * 1024 * 1024
CPU_IDLE = f"{BASE}.11.11.0"
MEM_TOTAL = f"{BASE}.4.5.0"
MEM_FREE = f"{BASE}.4.6.0"
MEM_AVAILABLE = f"{BASE}.4.27.0"
DISK_PATH = f"{BASE}.9.1.2"
DISK_AVAIL = f"{BASE}.9.1.7"
DISK_USED_PERCENT = f"{BASE}.9.1.9"
DISK_INODE_PERCENT = f"{BASE}.9.1.10"
HR_DESCR = "1.3.6.1.2.1.25.2.3.1.3"
HR_UNITS = "1.3.6.1.2.1.25.2.3.1.4"
HR_SIZE = "1.3.6.1.2.1.25.2.3.1.5"
HR_USED = "1.3.6.1.2.1.25.2.3.1.6"
IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
IF_IN = "1.3.6.1.2.1.31.1.1.1.6"
IF_OUT = "1.3.6.1.2.1.31.1.1.1.10"
IF_STATUS = "1.3.6.1.2.1.2.2.1.8"


def _number(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _usable(value) -> bool:
    return value is not None and not type(value).__name__.startswith(("NoSuch", "EndOfMib"))


class _SnmpReader:
    def __init__(self, engine, community, target):
        self.engine = engine
        self.auth = CommunityData(community, mpModel=1)
        self.target = target
        self.context = ContextData()

    async def get(self, oids: list[str]) -> dict[str, object | None]:
        if not oids:
            return {}
        error, status, index, binds = await get_cmd(
            self.engine, self.auth, self.target, self.context,
            *(ObjectType(ObjectIdentity(oid)) for oid in oids),
        )
        if error:
            raise RuntimeError(str(error))
        if status:
            raise RuntimeError(f"SNMP {status.prettyPrint()} at index {index}")
        if len(binds) != len(oids):
            raise RuntimeError("SNMP response is incomplete")
        return {oid: value if _usable(value) else None for oid, (_, value) in zip(oids, binds)}

    async def walk(self, prefix: str, limit: int = 32) -> dict[int, str]:
        rows: dict[int, str] = {}
        async for error, status, index, binds in walk_cmd(
            self.engine, self.auth, self.target, self.context,
            ObjectType(ObjectIdentity(prefix)), lexicographicMode=False,
            lookupMib=False, maxRows=limit,
        ):
            if error:
                raise RuntimeError(str(error))
            if status:
                raise RuntimeError(f"SNMP {status.prettyPrint()} at index {index}")
            for oid, value in binds:
                oid_text = oid.prettyPrint()
                if oid_text.startswith(prefix + ".") and _usable(value):
                    suffix = oid_text[len(prefix) + 1:]
                    if suffix.isdigit():
                        rows[int(suffix)] = value.prettyPrint()[:200]
        return rows


async def inspect_linux_host(host: str, port: int, community: str) -> dict:
    """一次按需巡检：标量 + 分区/网卡发现 + 一秒网卡速率采样。"""
    engine = SnmpEngine()
    warnings: list[str] = []
    result = {"host": host, "port": port, "kind": "linux_snmp", "ok": False,
              "connectivity": {"snmp_reachable": False}}
    try:
        target = await UdpTransportTarget.create((host, port), timeout=2, retries=1)
        reader = _SnmpReader(engine, community, target)
        basic = await reader.get([CPU_IDLE, MEM_TOTAL, MEM_FREE, MEM_AVAILABLE])
        result["connectivity"]["snmp_reachable"] = True
        idle = _number(basic[CPU_IDLE])
        total = _number(basic[MEM_TOTAL])
        free = _number(basic[MEM_FREE])
        available = _number(basic[MEM_AVAILABLE])
        result["cpu"] = {"idle_percent": idle, "utilization_percent": 100 - idle if idle is not None else None,
                         "window": "约过去一分钟"}
        result["memory"] = {"total_gib": round(total / KIB_PER_GIB, 3) if total is not None else None,
                            "free_gib": round(free / KIB_PER_GIB, 3) if free is not None else None,
                            "available_gib": round(available / KIB_PER_GIB, 3) if available is not None else None,
                            "free_percent": round(free / total * 100, 1) if total and free is not None else None,
                            "available_percent": round(available / total * 100, 1) if total and available is not None else None,
                            "note": "容量单位为 GiB；available_gib 估计可供新进程使用的内存，free_gib 仅为空闲内存"}
        if idle is None:
            warnings.append("CPU 空闲率 OID 不可用")
        if total is None or free is None:
            warnings.append("内存 OID 不可用")
        if available is None:
            warnings.append("memSysAvail OID 不可用，无法报告可用内存")

        disks: list[dict] = []
        try:
            for idx, path in (await reader.walk(DISK_PATH)).items():
                oids = [f"{DISK_AVAIL}.{idx}", f"{DISK_USED_PERCENT}.{idx}", f"{DISK_INODE_PERCENT}.{idx}"]
                data = await reader.get(oids)
                available_kb = _number(data[oids[0]])
                disks.append({"path": path, "index": idx,
                              "available_gib": round(available_kb / KIB_PER_GIB, 3) if available_kb is not None else None,
                              "used_percent": _number(data[oids[1]]), "inode_used_percent": _number(data[oids[2]]),
                              "source": "dskTable"})
            if not disks:
                for idx, path in (await reader.walk(HR_DESCR, limit=64)).items():
                    if not path.startswith("/"):
                        continue
                    oids = [f"{HR_UNITS}.{idx}", f"{HR_SIZE}.{idx}", f"{HR_USED}.{idx}"]
                    data = await reader.get(oids)
                    units, size, used = (_number(data[oid]) for oid in oids)
                    if units is None or size is None or used is None or units <= 0 or size <= 0:
                        continue
                    disks.append({"path": path, "index": idx,
                                  "available_gib": round(max(size - used, 0) * units / BYTES_PER_GIB, 3),
                                  "used_percent": round(used / size * 100, 1),
                                  "inode_used_percent": None, "source": "hrStorage"})
                if disks:
                    warnings.append("dskTable 未配置，分区来自 HOST-RESOURCES-MIB；inode 使用率不可用")
                else:
                    warnings.append("没有发现分区；可在 snmpd.conf 配置 disk / 或 includeAllDisks")
        except Exception as exc:
            warnings.append(f"分区查询失败：{str(exc).replace(community, '[redacted]')}")
        result["disks"] = disks

        interfaces: list[dict] = []
        try:
            names = await reader.walk(IF_NAME)
            names = {idx: name for idx, name in names.items() if name != "lo"}
            for idx, name in names.items():
                oids = [f"{IF_STATUS}.{idx}", f"{IF_IN}.{idx}", f"{IF_OUT}.{idx}"]
                data = await reader.get(oids)
                interfaces.append({"name": name, "index": idx, "up": _number(data[oids[0]]) == 1,
                                   "rx_bytes_total": _number(data[oids[1]]),
                                   "tx_bytes_total": _number(data[oids[2]])})
            if interfaces:
                start = asyncio.get_running_loop().time()
                await asyncio.sleep(1)
                elapsed = max(asyncio.get_running_loop().time() - start, 0.001)
                for item in interfaces:
                    idx = item["index"]
                    oids = [f"{IF_IN}.{idx}", f"{IF_OUT}.{idx}"]
                    data = await reader.get(oids)
                    for direction, oid, field in (("rx", oids[0], "rx_bytes_total"),
                                                   ("tx", oids[1], "tx_bytes_total")):
                        previous = item[field]
                        current = _number(data[oid])
                        item[f"{direction}_bytes_per_second"] = (
                            round((current - previous) / elapsed, 1)
                            if current is not None and previous is not None and current >= previous else None
                        )
                        item[field] = current
                if any(item["rx_bytes_total"] is None or item["tx_bytes_total"] is None for item in interfaces):
                    warnings.append("部分网卡不支持 64 位流量计数器")
            else:
                warnings.append("没有发现非 loopback 网卡")
        except Exception as exc:
            warnings.append(f"网卡查询失败：{str(exc).replace(community, '[redacted]')}")
        result["interfaces"] = interfaces
        result["ok"] = idle is not None or total is not None or bool(disks) or bool(interfaces)
        if not result["ok"]:
            result["error"] = "SNMP 已响应，但所需监控 OID 都不可用"
        if warnings:
            result["warnings"] = warnings
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc).replace(community, '[redacted]')}"
    finally:
        engine.close_dispatcher()
        result["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return result
