"""One-shot, read-only probes inspired by Zabbix item types."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from memory import ObservationMemory
from inventory import resolve_asset


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def probe_snmp(asset: str, config: dict) -> dict:
    observation = {"asset": asset, "kind": "snmp_v2c", "checked_at": now_utc()}
    community = os.getenv(config["community_env"])
    if not community:
        observation.update(ok=False, error=f"Environment variable {config['community_env']} is not set")
        return observation

    if config.get("profile") == "linux_host":
        try:
            from snmp_host import inspect_linux_host
            observation.update(await inspect_linux_host(config["host"], config.get("port", 161), community))
        except ImportError:
            observation.update(ok=False, error="PySNMP is not installed; run pip install -r requirements.txt")
        observation["asset"] = asset
        observation["kind"] = "snmp_v2c"
        return observation

    try:
        from pysnmp.hlapi.v3arch.asyncio import (
            CommunityData,
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
            UdpTransportTarget,
            get_cmd,
        )
    except ImportError:
        observation.update(ok=False, error="PySNMP is not installed; run pip install -r requirements.txt")
        return observation

    engine = SnmpEngine()
    try:
        target = await UdpTransportTarget.create(
            (config["host"], config.get("port", 161)), timeout=2, retries=1
        )
        result = await get_cmd(
            engine,
            CommunityData(community, mpModel=1),
            target,
            ContextData(),
            *(ObjectType(ObjectIdentity(oid)) for oid in config["checks"].values()),
        )
        error_indication, error_status, error_index, var_binds = result
        if error_indication:
            observation.update(ok=False, error=str(error_indication).replace(community, "[redacted]"))
        elif error_status:
            observation.update(ok=False, error=f"{error_status.prettyPrint()} at index {error_index}")
        elif len(var_binds) != len(config["checks"]):
            observation.update(ok=False, error="SNMP response did not contain every configured OID")
        else:
            values = {
                label: {"oid": oid, "value": var_bind[1].prettyPrint()[:400]}
                for (label, oid), var_bind in zip(config["checks"].items(), var_binds)
            }
            unsupported = [
                label for label, var_bind in zip(config["checks"], var_binds)
                if type(var_bind[1]).__name__.startswith("NoSuch")
            ]
            observation.update(ok=not unsupported, values=values)
            if unsupported:
                observation["error"] = "Unsupported OIDs: " + ", ".join(unsupported)
    except Exception as exc:
        observation.update(ok=False, error=f"{type(exc).__name__}: {str(exc).replace(community, '[redacted]')}")
    finally:
        engine.close_dispatcher()
    return observation


async def run_probe(asset: str, inventory: dict[str, dict], memory: ObservationMemory) -> dict:
    try:
        asset = resolve_asset(asset, inventory)
    except ValueError as exc:
        return {"asset": asset, "ok": False, "error": str(exc)}
    config = inventory[asset]
    kind = config["kind"]
    if kind != "snmp_v2c":
        return {"asset": asset, "ok": False, "error": f"Unsupported asset kind: {kind}"}
    observation = await probe_snmp(asset, config)
    memory.record(observation)
    return observation
