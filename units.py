"""Human-readable decimal capacity units for monitoring results."""

from __future__ import annotations

import re


BYTES_PER_GIB = 1024**3
_BINARY_UNITS = {"kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4}
_BINARY_SIZE = re.compile(
    r"(?<![A-Za-z0-9_.])(?P<number>\d[\d,]*(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    r"\s*(?P<unit>TiB|GiB|MiB|KiB)(?![A-Za-z])",
    re.IGNORECASE,
)


def format_bytes(value: int | float | None) -> str:
    if value is None:
        return "—"
    megabytes = float(value) / 1_000_000
    amount, unit = ((float(value) / 1_000_000_000, "GB")
                    if round(megabytes, 2) >= 1000 else (megabytes, "MB"))
    decimals = 1 if amount >= 100 else 2 if amount >= 1 else 3
    label = f"{amount:,.{decimals}f}"
    if "." in label:
        label = label.rstrip("0").rstrip(".")
    return f"{label} {unit}"


def format_gib(value: int | float | None) -> str:
    """Format legacy GiB observations as MB or GB without scientific notation."""
    return format_bytes(float(value) * BYTES_PER_GIB) if value is not None else "—"


def format_capacity_text(text: str) -> str:
    """Normalize binary capacity mentions in model answers and old chat messages."""
    def replace(match: re.Match[str]) -> str:
        number = float(match.group("number").replace(",", ""))
        return format_bytes(number * _BINARY_UNITS[match.group("unit").lower()])

    return _BINARY_SIZE.sub(replace, text)


def public_units(value):
    """Keep stored observations compatible while exposing MB/GB to the model."""
    if isinstance(value, dict):
        return {(key[:-4] + "_size" if key.endswith("_gib") else key):
                (format_gib(item) if key.endswith("_gib") else public_units(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [public_units(item) for item in value]
    if isinstance(value, str):
        return format_capacity_text(value).replace("_gib", "_size").replace("GiB", "MB/GB")
    return value
