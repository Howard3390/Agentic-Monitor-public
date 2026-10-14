"""Read-only Docker over SSH and MySQL over TCP for inventory services."""

from __future__ import annotations

import asyncio
import json
import os
import re

from inventory import resolve_asset
from memory import ObservationMemory
from probes import now_utc


_SIZE = re.compile(r"^([\d.]+)\s*([KMGT]?i?B|[kMGT]?B)$")
_MULTIPLIER = {
    "B": 1, "kB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4,
    "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4,
}


def _gib(value: str) -> float | None:
    match = _SIZE.fullmatch(value.strip())
    if not match:
        return None
    return round(float(match.group(1)) * _MULTIPLIER[match.group(2)] / 1024**3, 3)


def _connection(config: dict, service: str) -> tuple[str, int, str, str]:
    settings = config["services"][service]
    names = [settings[key] for key in ("host_env", "port_env", "user_env", "password_env")]
    values = [os.getenv(name) for name in names]
    missing = [name for name, value in zip(names, values) if not value]
    if missing:
        raise ValueError("Missing environment variable(s): " + ", ".join(missing))
    host, port_text, user, password = values
    if host != config["host"]:
        raise ValueError(f"{names[0]} must match this asset's inventory host")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError(f"{names[1]} must be a TCP port") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{names[1]} must be a TCP port")
    return host, port, user, password


def _ssh_command(client, command: str) -> str:
    _, stdout, stderr = client.exec_command(command, timeout=20)
    output = stdout.read(200_001)
    error = stderr.read(200_001)
    code = stdout.channel.recv_exit_status()
    if len(output) > 200_000 or len(error) > 200_000:
        raise RuntimeError("Docker output exceeds 200 KB")
    if code:
        raise RuntimeError(f"Docker command failed (exit {code}): {error.decode('utf-8', 'replace')[:300]}")
    return output.decode("utf-8", "replace").strip()


def _json_lines(value: str) -> list[dict]:
    return [json.loads(line) for line in value.splitlines() if line.strip()]


def _docker_check(host: str, port: int, user: str, password: str) -> dict:
    import paramiko

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    known_hosts = os.path.expanduser("~/.ssh/known_hosts")
    if os.path.isfile(known_hosts):
        client.load_host_keys(known_hosts)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(host, port=port, username=user, password=password, timeout=8,
                       auth_timeout=8, banner_timeout=8, look_for_keys=False, allow_agent=False)
        version = _ssh_command(client, "docker version --format '{{.Server.Version}}'")
        containers = _json_lines(_ssh_command(client, "docker ps -a --format '{{json .}}'"))
        stats = _json_lines(_ssh_command(client, "docker stats --no-stream --format '{{json .}}'"))
    finally:
        client.close()

    by_name = {row.get("Name"): row for row in stats}
    result = []
    for row in containers[:50]:
        name = row.get("Names", "")
        item = {"name": name, "image": row.get("Image"), "state": row.get("State"),
                "status": row.get("Status")}
        stat = by_name.get(name)
        if stat:
            memory = stat.get("MemUsage", "").split("/")
            item["cpu_percent"] = stat.get("CPUPerc")
            item["memory_used_gib"] = _gib(memory[0]) if memory else None
            item["memory_limit_gib"] = _gib(memory[1]) if len(memory) > 1 else None
            item["memory_percent"] = stat.get("MemPerc")
            item["pids"] = stat.get("PIDs")
        result.append(item)
    return {"docker_version": version, "container_count": len(containers),
            "running_count": sum(row.get("State") == "running" for row in containers),
            "containers": result, "truncated": len(containers) > 50}


_STATUS = ("Uptime", "Threads_connected", "Threads_running", "Questions", "Slow_queries",
           "Aborted_connects", "Connections", "Com_select", "Com_insert", "Com_update",
           "Com_delete", "Innodb_buffer_pool_reads", "Innodb_buffer_pool_read_requests",
           "Innodb_buffer_pool_bytes_data")
_SYSTEM_SCHEMAS = ("information_schema", "mysql", "performance_schema", "sys")
_MYSQL_DETAIL_LIMIT = 50


def _size_gib(value: int | None) -> float:
    return round(int(value or 0) / 1024**3, 6)


def _mysql_catalog(cursor) -> dict:
    """Discover visible user databases and bounded table/view details."""
    excluded = ",".join("%s" for _ in _SYSTEM_SCHEMAS)
    cursor.execute(
        f"SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
        f"WHERE SCHEMA_NAME NOT IN ({excluded}) ORDER BY SCHEMA_NAME",
        _SYSTEM_SCHEMAS,
    )
    names = [row[0] for row in cursor.fetchall()]

    cursor.execute(
        f"SELECT TABLE_SCHEMA, SUM(TABLE_TYPE = 'BASE TABLE'), SUM(TABLE_TYPE = 'VIEW'), "
        f"COALESCE(SUM(CASE WHEN TABLE_TYPE = 'BASE TABLE' THEN TABLE_ROWS ELSE 0 END), 0), "
        f"COALESCE(SUM(DATA_LENGTH), 0), COALESCE(SUM(INDEX_LENGTH), 0) "
        f"FROM information_schema.TABLES WHERE TABLE_SCHEMA NOT IN ({excluded}) GROUP BY TABLE_SCHEMA",
        _SYSTEM_SCHEMAS,
    )
    summaries = {
        name: {"name": name, "table_count": int(table_count or 0),
               "view_count": int(view_count or 0), "estimated_rows": int(rows or 0),
               "data_gib": _size_gib(data_bytes), "index_gib": _size_gib(index_bytes),
               "total_gib": _size_gib(int(data_bytes or 0) + int(index_bytes or 0))}
        for name, table_count, view_count, rows, data_bytes, index_bytes in cursor.fetchall()
    }
    databases = [summaries.get(name, {"name": name, "table_count": 0, "view_count": 0,
                                      "estimated_rows": 0, "data_gib": 0.0,
                                      "index_gib": 0.0, "total_gib": 0.0}) for name in names]

    cursor.execute(
        f"SELECT TABLE_SCHEMA, TABLE_NAME, ENGINE, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH "
        f"FROM information_schema.TABLES WHERE TABLE_SCHEMA NOT IN ({excluded}) "
        f"AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_SCHEMA, TABLE_NAME LIMIT %s",
        (*_SYSTEM_SCHEMAS, _MYSQL_DETAIL_LIMIT + 1),
    )
    table_rows = cursor.fetchall()
    tables = [
        {"database": database, "name": name, "engine": engine,
         "estimated_rows": int(rows) if rows is not None else None,
         "data_gib": _size_gib(data_bytes), "index_gib": _size_gib(index_bytes),
         "total_gib": _size_gib(int(data_bytes or 0) + int(index_bytes or 0))}
        for database, name, engine, rows, data_bytes, index_bytes in table_rows[:_MYSQL_DETAIL_LIMIT]
    ]

    cursor.execute(
        f"SELECT TABLE_SCHEMA, TABLE_NAME FROM information_schema.TABLES "
        f"WHERE TABLE_SCHEMA NOT IN ({excluded}) AND TABLE_TYPE = 'VIEW' "
        f"ORDER BY TABLE_SCHEMA, TABLE_NAME LIMIT %s",
        (*_SYSTEM_SCHEMAS, _MYSQL_DETAIL_LIMIT + 1),
    )
    view_rows = cursor.fetchall()
    return {"database_count": len(databases), "table_count": sum(row["table_count"] for row in databases),
            "view_count": sum(row["view_count"] for row in databases), "databases": databases,
            "tables": tables, "tables_truncated": len(table_rows) > _MYSQL_DETAIL_LIMIT,
            "views": [{"database": database, "name": name}
                      for database, name in view_rows[:_MYSQL_DETAIL_LIMIT]],
            "views_truncated": len(view_rows) > _MYSQL_DETAIL_LIMIT}


def _mysql_check(host: str, port: int, user: str, password: str) -> dict:
    import pymysql

    connection = pymysql.connect(host=host, port=port, user=user, password=password,
                                 connect_timeout=6, read_timeout=8, write_timeout=8,
                                 charset="utf8mb4")
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT VERSION(), @@hostname, @@read_only")
            version, server_host, read_only = cursor.fetchone()
            placeholders = ",".join("%s" for _ in _STATUS)
            cursor.execute(f"SHOW GLOBAL STATUS WHERE Variable_name IN ({placeholders})", _STATUS)
            status = {key: int(value) for key, value in cursor.fetchall()}
            cursor.execute("SHOW GLOBAL VARIABLES LIKE 'max_connections'")
            row = cursor.fetchone()
            max_connections = int(row[1]) if row else None
            try:
                catalog = _mysql_catalog(cursor)
            except pymysql.MySQLError as exc:
                catalog = {"metadata_error": type(exc).__name__}
    finally:
        connection.close()

    reads = status.get("Innodb_buffer_pool_reads", 0)
    requests = status.get("Innodb_buffer_pool_read_requests", 0)
    buffer_bytes = status.pop("Innodb_buffer_pool_bytes_data", None)
    return {"version": version, "server_host": server_host, "endpoint": f"{host}:{port}",
            "read_only": bool(read_only), **catalog,
            "status": status, "max_connections": max_connections,
            "connection_usage_percent": round(status["Threads_connected"] / max_connections * 100, 2)
            if max_connections else None,
            "innodb_buffer_pool_data_gib": round(buffer_bytes / 1024**3, 3) if buffer_bytes is not None else None,
            "innodb_buffer_pool_hit_percent": round((1 - reads / requests) * 100, 2)
            if requests else None}


async def run_service_probe(reference: str, service: str, inventory: dict[str, dict],
                            memory: ObservationMemory) -> dict:
    try:
        asset = resolve_asset(reference, inventory)
    except ValueError as exc:
        return {"asset": reference, "kind": service, "ok": False, "error": str(exc)}
    config = inventory[asset]
    if service not in config.get("services", {}):
        return {"asset": asset, "kind": service, "ok": False,
                "error": f"{service} is not configured for this asset"}

    observation = {"asset": asset, "kind": service, "host": config["host"], "checked_at": now_utc()}
    try:
        host, port, user, password = _connection(config, service)
        check = _docker_check if service == "docker" else _mysql_check
        observation.update(await asyncio.to_thread(check, host, port, user, password))
        observation["ok"] = True
    except ImportError:
        observation.update(ok=False, error=f"{service} dependency is not installed; run pip install -r requirements.txt")
    except Exception as exc:
        # Driver and SSH exceptions can include connection details. Never return raw exception text.
        observation.update(ok=False, error=f"{service} check failed: {type(exc).__name__}")
    memory.record(observation)
    return observation
