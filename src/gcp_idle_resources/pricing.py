"""Monthly cost estimates.

The numbers below are EXAMPLE DEFAULTS in USD, roughly in line with public
list prices for a US region at the time of writing. They are not exact, they
differ by region, and they ignore discounts, free tiers and committed-use
contracts. Override them with ``--prices FILE`` to match your own bill.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .inventory import InputError

HOURS_PER_MONTH = 730

DEFAULT_PRICES: dict[str, Any] = {
    "currency": "USD",
    # persistent disk capacity, per GiB per month, keyed by disk type name
    "disk_gib_month": {
        "pd-standard": 0.04,
        "pd-balanced": 0.10,
        "pd-ssd": 0.17,
        "pd-extreme": 0.125,
        "default": 0.10,
    },
    # regional disks keep two replicas and are billed at about twice the zonal rate
    "regional_disk_multiplier": 2.0,
    # snapshot storage, per GiB of storageBytes per month
    "snapshot_gib_month": 0.05,
    "archive_snapshot_gib_month": 0.019,
    # static external IPv4 addresses, per hour
    "static_ip_unused_hour": 0.010,
    "static_ip_in_use_hour": 0.005,
}


def load_prices(path: Path | None) -> dict[str, Any]:
    prices = copy.deepcopy(DEFAULT_PRICES)
    if path is None:
        return prices
    try:
        override = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InputError(f"cannot read price file {path}: {exc.strerror or exc}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"price file {path} is not valid JSON ({exc.msg})") from exc
    if not isinstance(override, dict):
        raise InputError(f"price file {path} must be a JSON object")
    for key, value in override.items():
        if key not in prices:
            raise InputError(f"unknown price key {key!r} in {path}; known keys: {', '.join(sorted(prices))}")
        if isinstance(prices[key], dict):
            if not isinstance(value, dict):
                raise InputError(f"price key {key!r} must be an object")
            prices[key].update({k: float(v) for k, v in value.items()})
        elif key == "currency":
            prices[key] = str(value)
        else:
            prices[key] = float(value)
    return prices


def disk_month(prices: dict[str, Any], disk_type: str, size_gb: int | None, regional: bool) -> float | None:
    if size_gb is None:
        return None
    table = prices["disk_gib_month"]
    rate = table.get(disk_type, table.get("default", 0.0))
    if regional:
        rate *= prices["regional_disk_multiplier"]
    return round(rate * size_gb, 2)


def snapshot_month(prices: dict[str, Any], storage_bytes: int | None, archive: bool) -> float | None:
    if storage_bytes is None:
        return None
    rate = prices["archive_snapshot_gib_month"] if archive else prices["snapshot_gib_month"]
    return round(rate * storage_bytes / 1024**3, 2)


def ip_month(prices: dict[str, Any], in_use: bool) -> float:
    rate = prices["static_ip_in_use_hour"] if in_use else prices["static_ip_unused_hour"]
    return round(rate * HOURS_PER_MONTH, 2)
