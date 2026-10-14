"""Persist a small amount of observed data, never credentials or model guesses."""

from __future__ import annotations

import json
import os
from pathlib import Path


class ObservationMemory:
    def __init__(self, path: Path, per_asset_limit: int = 20):
        self.path = path
        self.per_asset_limit = per_asset_limit

    def observation_history(self, asset: str) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"Cannot read observation memory: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("Observation memory must be a JSON object")
        entries = data.get(asset, [])
        if not isinstance(entries, list):
            raise ValueError(f"Invalid observation memory for {asset}")
        return entries

    def last(self, asset: str) -> dict | None:
        entries = self.observation_history(asset)
        return entries[-1] if entries else None

    def record(self, observation: dict) -> None:
        asset = observation["asset"]
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise ValueError(f"Cannot update observation memory: {exc}") from exc
            if not isinstance(data, dict):
                raise ValueError("Observation memory must be a JSON object")
        else:
            data = {}
        entries = data.get(asset, [])
        if not isinstance(entries, list):
            raise ValueError(f"Invalid observation memory for {asset}")
        data[asset] = (entries + [observation])[-self.per_asset_limit:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
