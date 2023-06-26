"""Load gcloud JSON exports and normalise the fields the checks need.

The input files are the output of, for example::

    gcloud compute disks list --format=json > disks.json

Field names follow the Compute Engine v1 REST resources (Disk, Address,
Snapshot, Instance). int64 fields such as ``sizeGb`` arrive as JSON strings,
so every numeric field is parsed defensively.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

KINDS = ("disks", "addresses", "snapshots", "instances")
_FRACTION = re.compile(r"\.(\d+)(?=[+-]\d\d:\d\d$|$)")


class InputError(Exception):
    """Raised when an input file is missing or is not a gcloud JSON list."""


@dataclass
class Inventory:
    disks: list[dict[str, Any]] = field(default_factory=list)
    addresses: list[dict[str, Any]] = field(default_factory=list)
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    instances: list[dict[str, Any]] = field(default_factory=list)
    loaded: list[str] = field(default_factory=list)

    def has(self, kind: str) -> bool:
        return kind in self.loaded


def load_json_list(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror or exc}") from exc
    text = text.strip()
    if not text:
        return []  # gcloud prints nothing at all for an empty list in some versions
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError(f"{path} is not valid JSON ({exc.msg} at line {exc.lineno})") from exc
    if isinstance(data, dict):
        # tolerate a single `describe` object or a raw API page with "items"
        if isinstance(data.get("items"), list):
            data = data["items"]
        else:
            data = [data]
    if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
        raise InputError(f"{path} must contain a JSON list of objects (gcloud ... list --format=json)")
    return data


def load_inventory(paths: dict[str, Path | None]) -> Inventory:
    inv = Inventory()
    for kind in KINDS:
        p = paths.get(kind)
        if p is None:
            continue
        setattr(inv, kind, load_json_list(p))
        inv.loaded.append(kind)
    return inv


def find_in_dir(directory: Path) -> dict[str, Path | None]:
    """Map each resource kind to ``<dir>/<kind>.json`` when that file exists."""
    if not directory.is_dir():
        raise InputError(f"{directory} is not a directory")
    found: dict[str, Path | None] = {}
    for kind in KINDS:
        p = directory / f"{kind}.json"
        found[kind] = p if p.is_file() else None
    if not any(found.values()):
        raise InputError(f"no disks.json, addresses.json, snapshots.json or instances.json in {directory}")
    return found


# ---------------------------------------------------------------- helpers


def last_segment(url: str | None) -> str:
    """'https://.../zones/us-central1-a' -> 'us-central1-a'."""
    if not url:
        return ""
    return url.rstrip("/").rsplit("/", 1)[-1]


def to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_ts(value: Any) -> datetime | None:
    """Parse an RFC3339 timestamp as written by Compute Engine."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00").replace("z", "+00:00")
    # Python 3.10 only accepts 3 or 6 fractional digits; normalise to 6.
    m = _FRACTION.search(text)
    if m:
        text = text[: m.start()] + "." + m.group(1)[:6].ljust(6, "0") + text[m.end():]
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def age_days(ts: datetime | None, now: datetime) -> int | None:
    if ts is None:
        return None
    return max(0, (now - ts).days)


def location_of(resource: dict[str, Any]) -> tuple[str, str]:
    """Return ('zone'|'region'|'global', name) for a disk, address or instance."""
    if resource.get("zone"):
        return "zone", last_segment(resource["zone"])
    if resource.get("region"):
        return "region", last_segment(resource["region"])
    return "global", ""


def resource_key(url: str | None) -> str:
    """Normalise a resource URL so full and partial links compare equal.

    'https://www.googleapis.com/compute/v1/projects/p/zones/z/disks/d' and
    'projects/p/zones/z/disks/d' both become 'projects/p/zones/z/disks/d'.
    """
    if not url:
        return ""
    idx = url.find("projects/")
    return url[idx:] if idx >= 0 else url
