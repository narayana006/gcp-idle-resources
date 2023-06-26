"""The idle-resource checks. Pure functions over an Inventory; nothing here talks to Google Cloud."""

from __future__ import annotations

import shlex
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from . import pricing
from .inventory import Inventory, age_days, last_segment, location_of, parse_ts, resource_key, to_int

CHECKS = ("disks", "addresses", "snapshots", "instances")

# Reserved internal ranges with these purposes back managed services (private
# services access, Private Service Connect) and are "unused" by design.
SERVICE_RANGE_PURPOSES = {"VPC_PEERING", "PRIVATE_SERVICE_CONNECT"}


@dataclass
class Finding:
    check: str
    name: str
    project: str
    location: str  # "zone/us-central1-a", "region/us-central1" or "global"
    reason: str
    monthly_cost: float | None
    age_days: int | None
    command: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Options:
    min_idle_days: int = 7
    snapshot_age_days: int = 90
    include_internal: bool = False
    checks: tuple[str, ...] = CHECKS


def project_of(resource: dict[str, Any]) -> str:
    link = resource.get("selfLink") or ""
    parts = link.split("/")
    if "projects" in parts:
        i = parts.index("projects")
        if i + 1 < len(parts):
            return parts[i + 1]
    return ""


def _gcloud(*args: str, project: str) -> str:
    cmd = ["gcloud", "compute", *args]
    if project:
        cmd.append(f"--project={project}")
    return " ".join(shlex.quote(a) for a in cmd)


def _loc_flag(scope: str, loc: str) -> str:
    if scope == "zone":
        return f"--zone={loc}"
    if scope == "region":
        return f"--region={loc}"
    return "--global"


def _loc_label(scope: str, loc: str) -> str:
    return f"{scope}/{loc}" if loc else scope


# ------------------------------------------------------------------ disks


def check_disks(inv: Inventory, prices: dict[str, Any], opts: Options, now: datetime) -> list[Finding]:
    out: list[Finding] = []
    for d in inv.disks:
        if d.get("users"):
            continue
        name = d.get("name", "?")
        scope, loc = location_of(d)
        size = to_int(d.get("sizeGb"))
        dtype = last_segment(d.get("type")) or "unknown"
        detached = parse_ts(d.get("lastDetachTimestamp"))
        created = parse_ts(d.get("creationTimestamp"))
        since = detached or created
        idle = age_days(since, now)
        if idle is not None and idle < opts.min_idle_days:
            continue
        if detached:
            why = f"unattached {dtype} disk, {size} GB, detached {idle} days ago"
        elif d.get("lastAttachTimestamp"):
            why = f"unattached {dtype} disk, {size} GB"
        else:
            why = f"{dtype} disk, {size} GB, never attached (created {idle} days ago)"
        if scope == "region":
            why += ", regional (two replicas)"
        out.append(Finding(
            check="disks",
            name=name,
            project=project_of(d),
            location=_loc_label(scope, loc),
            reason=why,
            monthly_cost=pricing.disk_month(prices, dtype, size, scope == "region"),
            age_days=idle,
            command=_gcloud("disks", "delete", name, _loc_flag(scope, loc), project=project_of(d)),
            details={"type": dtype, "sizeGb": size, "status": d.get("status")},
        ))
    return out


# -------------------------------------------------------------- addresses


def check_addresses(inv: Inventory, prices: dict[str, Any], opts: Options, now: datetime) -> list[Finding]:
    out: list[Finding] = []
    for a in inv.addresses:
        if a.get("status") != "RESERVED" or a.get("users"):
            continue  # IN_USE, or still RESERVING
        atype = a.get("addressType") or "EXTERNAL"  # API default is EXTERNAL
        purpose = a.get("purpose") or ""
        if atype != "EXTERNAL":
            if not opts.include_internal or purpose in SERVICE_RANGE_PURPOSES:
                continue
        name = a.get("name", "?")
        scope, loc = location_of(a)
        ip = a.get("address", "")
        version = a.get("ipVersion") or "IPV4"
        created_age = age_days(parse_ts(a.get("creationTimestamp")), now)
        if created_age is not None and created_age < opts.min_idle_days:
            continue
        cost: float | None = None
        if atype == "EXTERNAL" and version == "IPV4":
            cost = pricing.ip_month(prices, in_use=False)
            why = f"static external IP {ip} reserved but not used by anything"
        elif atype == "EXTERNAL":
            why = f"static external {version} address {ip} reserved but not used (cost not estimated)"
        else:
            why = f"static internal IP {ip} reserved but not used (holds an address in the subnet)"
        out.append(Finding(
            check="addresses",
            name=name,
            project=project_of(a),
            location=_loc_label(scope, loc),
            reason=why,
            monthly_cost=cost,
            age_days=created_age,
            command=_gcloud("addresses", "delete", name, _loc_flag(scope, loc), project=project_of(a)),
            details={"address": ip, "addressType": atype, "networkTier": a.get("networkTier"),
                     "purpose": purpose or None},
        ))
    return out


# -------------------------------------------------------------- snapshots


def check_snapshots(inv: Inventory, prices: dict[str, Any], opts: Options, now: datetime) -> list[Finding]:
    known_disks = {resource_key(d.get("selfLink")) for d in inv.disks if d.get("selfLink")}
    out: list[Finding] = []
    for s in inv.snapshots:
        age = age_days(parse_ts(s.get("creationTimestamp")), now)
        if age is None or age < opts.snapshot_age_days:
            continue
        name = s.get("name", "?")
        stored = to_int(s.get("storageBytes"))
        disk_gb = to_int(s.get("diskSizeGb"))
        archive = s.get("snapshotType") == "ARCHIVE"
        parts = [f"{age} days old"]
        if stored is not None:
            parts.append(f"{stored / 1024**3:.1f} GiB stored")
        if disk_gb is not None:
            parts.append(f"source disk {disk_gb} GB")
        if archive:
            parts.append("archive snapshot")
        source = s.get("sourceDisk")
        if inv.has("disks") and source and resource_key(source) not in known_disks:
            parts.append(f"source disk {last_segment(source)} no longer exists")
        if s.get("autoCreated"):
            parts.append("created by a snapshot schedule")
        out.append(Finding(
            check="snapshots",
            name=name,
            project=project_of(s),
            location="global",
            reason=", ".join(parts),
            monthly_cost=pricing.snapshot_month(prices, stored, archive),
            age_days=age,
            command=_gcloud("snapshots", "delete", name, project=project_of(s)),
            details={"storageBytes": stored, "diskSizeGb": disk_gb, "sourceDisk": source,
                     "storageLocations": s.get("storageLocations")},
        ))
    return out


# -------------------------------------------------------------- instances


def check_instances(inv: Inventory, prices: dict[str, Any], opts: Options, now: datetime) -> list[Finding]:
    disks_by_key = {resource_key(d.get("selfLink")): d for d in inv.disks if d.get("selfLink")}
    static_by_ip = {a.get("address"): a for a in inv.addresses if a.get("status") == "IN_USE" and a.get("address")}
    out: list[Finding] = []
    for vm in inv.instances:
        status = vm.get("status")
        if status not in ("TERMINATED", "SUSPENDED"):
            continue
        stamp = vm.get("lastSuspendedTimestamp") if status == "SUSPENDED" else None
        stopped = parse_ts(stamp or vm.get("lastStopTimestamp"))
        idle = age_days(stopped, now)
        if idle is not None and idle < opts.min_idle_days:
            continue
        name = vm.get("name", "?")
        scope, loc = location_of(vm)
        cost = 0.0
        unknown = False
        disk_names: list[str] = []
        total_gb = 0
        for att in vm.get("disks") or []:
            if att.get("type", "PERSISTENT") != "PERSISTENT":
                continue  # local SSD (SCRATCH) is not kept for a stopped VM
            disk = disks_by_key.get(resource_key(att.get("source")))
            if disk is not None:
                size = to_int(disk.get("sizeGb"))
                dtype = last_segment(disk.get("type")) or "unknown"
                regional = bool(disk.get("region"))
            else:
                size = to_int(att.get("diskSizeGb"))
                dtype, regional = "default", False
            disk_names.append(last_segment(att.get("source")) or att.get("deviceName", "?"))
            c = pricing.disk_month(prices, dtype, size, regional)
            if c is None:
                unknown = True
            else:
                cost += c
                total_gb += size or 0
        ips: list[str] = []
        for nic in vm.get("networkInterfaces") or []:
            for ac in nic.get("accessConfigs") or []:
                ip = ac.get("natIP")
                if ip and ip in static_by_ip:
                    ips.append(ip)
                    cost += pricing.ip_month(prices, in_use=True)
        what = "stopped" if status == "TERMINATED" else "suspended"
        why = f"{what} {idle} days ago" if idle is not None else f"{what} (stop time unknown)"
        why += f"; {len(disk_names)} persistent disk(s), {total_gb} GB"
        if unknown:
            why += " (some sizes unknown)"
        if ips:
            why += f"; static IP {', '.join(ips)} still reserved"
        if status == "SUSPENDED":
            why += "; preserved memory state is billed too (not estimated)"
        out.append(Finding(
            check="instances",
            name=name,
            project=project_of(vm),
            location=_loc_label(scope, loc),
            reason=why,
            monthly_cost=round(cost, 2) if disk_names or ips else None,
            age_days=idle,
            command=_gcloud("instances", "delete", name, _loc_flag(scope, loc), project=project_of(vm)),
            details={"status": status, "machineType": last_segment(vm.get("machineType")),
                     "disks": disk_names, "staticIPs": ips,
                     "autoDeleteDisks": [last_segment(a.get("source")) for a in vm.get("disks") or []
                                         if a.get("autoDelete")]},
        ))
    return out


RUNNERS = {
    "disks": check_disks,
    "addresses": check_addresses,
    "snapshots": check_snapshots,
    "instances": check_instances,
}


def run_checks(inv: Inventory, prices: dict[str, Any], opts: Options, now: datetime) -> list[Finding]:
    findings: list[Finding] = []
    for check in opts.checks:
        if inv.has(check):
            findings.extend(RUNNERS[check](inv, prices, opts, now))
    findings.sort(key=lambda f: (CHECKS.index(f.check), -(f.monthly_cost or 0), f.name))
    return findings
