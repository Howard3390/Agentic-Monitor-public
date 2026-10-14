"""Small, explicit inventory. The model can only query targets listed here."""

from __future__ import annotations

import json
import re
from pathlib import Path


ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
NUMERIC_OID = re.compile(r"^\d+(?:\.\d+)+$")


def load_inventory(path: Path) -> dict[str, dict]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Inventory not found: {path}. Copy inventory.example.json to inventory.json.") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid inventory JSON: {exc}") from exc

    assets = raw.get("assets") if isinstance(raw, dict) else None
    if not isinstance(assets, dict) or not assets:
        raise ValueError("Inventory must contain a non-empty 'assets' object")

    for name, config in assets.items():
        if not isinstance(name, str) or not name or not isinstance(config, dict):
            raise ValueError("Each asset needs a non-empty name and an object configuration")
        kind = config.get("kind")
        if kind == "snmp_v2c":
            if not isinstance(config.get("host"), str) or not config["host"]:
                raise ValueError(f"{name}: host is required")
            port = config.get("port", 161)
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError(f"{name}: port must be between 1 and 65535")
            community_env = config.get("community_env")
            if not isinstance(community_env, str) or not ENV_NAME.fullmatch(community_env):
                raise ValueError(f"{name}: community_env must name an environment variable")
            profile = config.get("profile")
            if profile not in (None, "linux_host"):
                raise ValueError(f"{name}: unsupported SNMP profile {profile!r}")
            checks = config.get("checks")
            if profile is None:
                if not isinstance(checks, dict) or not checks or len(checks) > 20:
                    raise ValueError(f"{name}: checks must contain 1 to 20 named OIDs")
            elif checks is not None and (not isinstance(checks, dict) or len(checks) > 20):
                raise ValueError(f"{name}: checks must contain at most 20 named OIDs")
            if checks is not None and any(
                not isinstance(label, str) or not label or not isinstance(oid, str) or not NUMERIC_OID.fullmatch(oid)
                for label, oid in checks.items()
            ):
                raise ValueError(f"{name}: checks must map names to numeric OIDs")
            services = config.get("services", {})
            if not isinstance(services, dict) or any(service not in ("docker", "mysql") for service in services):
                raise ValueError(f"{name}: services may only contain docker and mysql")
            for service, settings in services.items():
                required = {"host_env", "port_env", "user_env", "password_env"}
                if not isinstance(settings, dict) or set(settings) != required or any(
                    not isinstance(settings[key], str) or not ENV_NAME.fullmatch(settings[key])
                    for key in required
                ):
                    raise ValueError(f"{name}: {service} needs four environment variable names")
        else:
            raise ValueError(f"{name}: unsupported kind {kind!r}")

    return assets


def resolve_asset(reference: str, inventory: dict[str, dict]) -> str:
    """允许按清单名称或清单内唯一 IP 选择资产；禁止任意目标。"""
    if reference in inventory:
        return reference
    matches = [name for name, config in inventory.items() if config.get("host") == reference]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"IP {reference} 对应多个资产，请使用资产名")
    raise ValueError("Asset is not in the inventory")
