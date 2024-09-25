from datetime import datetime, timezone

import pytest

from gcp_idle_resources.checks import Options, check_addresses, check_disks, check_instances, check_snapshots, run_checks
from gcp_idle_resources.inventory import Inventory, parse_ts, resource_key
from gcp_idle_resources.pricing import DEFAULT_PRICES

NOW = datetime(2025, 11, 10, tzinfo=timezone.utc)
B = "https://www.googleapis.com/compute/v1/projects/demo-1"
PRICES = DEFAULT_PRICES


def inv(**kinds):
    i = Inventory(**kinds)
    i.loaded = list(kinds)
    return i


def disk(name, size="100", dtype="pd-standard", users=None, **extra):
    d = {"name": name, "sizeGb": size, "zone": f"{B}/zones/us-east1-b",
         "type": f"{B}/zones/us-east1-b/diskTypes/{dtype}",
         "selfLink": f"{B}/zones/us-east1-b/disks/{name}",
         "creationTimestamp": "2025-01-01T00:00:00.000-08:00"}
    if users:
        d["users"] = users
    d.update(extra)
    return d


# ------------------------------------------------------------------ disks

def test_attached_disk_is_not_reported():
    i = inv(disks=[disk("a", users=[f"{B}/zones/us-east1-b/instances/vm"])])
    assert check_disks(i, PRICES, Options(), NOW) == []


def test_unattached_disk_cost_and_command():
    i = inv(disks=[disk("a", size="500", lastDetachTimestamp="2025-06-01T00:00:00Z")])
    [f] = check_disks(i, PRICES, Options(), NOW)
    assert f.monthly_cost == 20.0  # 500 GB * 0.04
    assert f.age_days == 162
    assert "detached 162 days ago" in f.reason
    assert f.command == "gcloud compute disks delete a --zone=us-east1-b --project=demo-1"


def test_recently_detached_disk_is_skipped_until_min_idle_days():
    i = inv(disks=[disk("a", lastDetachTimestamp="2025-11-08T00:00:00Z")])
    assert check_disks(i, PRICES, Options(min_idle_days=7), NOW) == []
    assert len(check_disks(i, PRICES, Options(min_idle_days=0), NOW)) == 1


def test_never_attached_disk_uses_creation_time():
    [f] = check_disks(inv(disks=[disk("a")]), PRICES, Options(), NOW)
    assert "never attached" in f.reason


def test_regional_disk_doubles_price_and_uses_region_flag():
    d = disk("r", size="100", dtype="pd-ssd")
    del d["zone"]
    d["region"] = f"{B}/regions/us-east1"
    [f] = check_disks(inv(disks=[d]), PRICES, Options(), NOW)
    assert f.monthly_cost == 34.0
    assert "--region=us-east1" in f.command
    assert f.location == "region/us-east1"


def test_unknown_disk_type_uses_default_price_and_numeric_size():
    [f] = check_disks(inv(disks=[disk("h", size=10, dtype="hyperdisk-balanced")]), PRICES, Options(), NOW)
    assert f.monthly_cost == 1.0


# -------------------------------------------------------------- addresses

def addr(name, status="RESERVED", **extra):
    a = {"name": name, "address": "203.0.113.5", "status": status, "region": f"{B}/regions/us-east1",
         "selfLink": f"{B}/regions/us-east1/addresses/{name}", "creationTimestamp": "2025-01-01T00:00:00Z"}
    a.update(extra)
    return a


def test_reserved_external_address_is_reported():
    [f] = check_addresses(inv(addresses=[addr("ip")]), PRICES, Options(), NOW)
    assert f.monthly_cost == 7.3
    assert f.command.endswith("--region=us-east1 --project=demo-1")


def test_in_use_and_reserving_addresses_are_ignored():
    i = inv(addresses=[addr("a", "IN_USE", users=["x"]), addr("b", "RESERVING")])
    assert check_addresses(i, PRICES, Options(), NOW) == []


def test_global_address_uses_global_flag():
    a = addr("g")
    del a["region"]
    a["selfLink"] = f"{B}/global/addresses/g"
    [f] = check_addresses(inv(addresses=[a]), PRICES, Options(), NOW)
    assert "--global" in f.command and f.location == "global"


def test_internal_addresses_only_with_flag_and_never_service_ranges():
    i = inv(addresses=[addr("vip", addressType="INTERNAL"),
                       addr("psa", addressType="INTERNAL", purpose="VPC_PEERING")])
    assert check_addresses(i, PRICES, Options(), NOW) == []
    found = check_addresses(i, PRICES, Options(include_internal=True), NOW)
    assert [f.name for f in found] == ["vip"]
    assert found[0].monthly_cost is None


def test_ipv6_external_has_no_cost_estimate():
    [f] = check_addresses(inv(addresses=[addr("v6", ipVersion="IPV6")]), PRICES, Options(), NOW)
    assert f.monthly_cost is None


# -------------------------------------------------------------- snapshots

def snap(name, created, stored=str(10 * 1024**3), **extra):
    s = {"name": name, "creationTimestamp": created, "storageBytes": stored, "diskSizeGb": "50",
         "sourceDisk": f"{B}/zones/us-east1-b/disks/src", "selfLink": f"{B}/global/snapshots/{name}"}
    s.update(extra)
    return s


def test_snapshot_age_threshold():
    i = inv(snapshots=[snap("old", "2025-01-01T00:00:00Z"), snap("new", "2025-11-01T00:00:00Z")])
    found = check_snapshots(i, PRICES, Options(snapshot_age_days=90), NOW)
    assert [f.name for f in found] == ["old"]
    assert found[0].monthly_cost == 0.5


def test_snapshot_reports_deleted_source_disk_only_when_disks_loaded():
    s = snap("old", "2024-01-02T00:00:00Z")
    without = check_snapshots(inv(snapshots=[s]), PRICES, Options(), NOW)[0]
    assert "no longer exists" not in without.reason
    withd = check_snapshots(inv(snapshots=[s], disks=[disk("other")]), PRICES, Options(), NOW)[0]
    assert "source disk src no longer exists" in withd.reason
    present = check_snapshots(inv(snapshots=[s], disks=[disk("src")]), PRICES, Options(), NOW)[0]
    assert "no longer exists" not in present.reason


def test_archive_snapshot_price():
    s = snap("arch", "2024-01-02T00:00:00Z", stored=str(100 * 1024**3), snapshotType="ARCHIVE")
    [f] = check_snapshots(inv(snapshots=[s]), PRICES, Options(), NOW)
    assert f.monthly_cost == 1.9


# -------------------------------------------------------------- instances

def vm(name, status, **extra):
    v = {"name": name, "status": status, "zone": f"{B}/zones/us-east1-b",
         "selfLink": f"{B}/zones/us-east1-b/instances/{name}",
         "machineType": f"{B}/zones/us-east1-b/machineTypes/e2-small",
         "lastStopTimestamp": "2025-08-01T00:00:00Z",
         "disks": [{"type": "PERSISTENT", "source": f"{B}/zones/us-east1-b/disks/{name}", "boot": True,
                    "autoDelete": True, "diskSizeGb": "10"},
                   {"type": "SCRATCH", "diskSizeGb": "375"}],
         "networkInterfaces": [{"accessConfigs": [{"type": "ONE_TO_ONE_NAT", "natIP": "203.0.113.9"}]}]}
    v.update(extra)
    return v


def test_running_vm_is_ignored():
    assert check_instances(inv(instances=[vm("a", "RUNNING")]), PRICES, Options(), NOW) == []


def test_stopped_vm_without_disk_inventory_uses_attached_size():
    [f] = check_instances(inv(instances=[vm("a", "TERMINATED")]), PRICES, Options(), NOW)
    assert f.monthly_cost == 1.0  # 10 GB at the default rate; ephemeral IP and local SSD not billed
    assert f.details["autoDeleteDisks"] == ["a"]
    assert "stopped 101 days ago" in f.reason


def test_stopped_vm_counts_static_ip_and_real_disk_type():
    d = disk("a", size="100", dtype="pd-ssd", users=["x"])
    a = addr("ip", "IN_USE", address="203.0.113.9", users=["x"])
    i = inv(instances=[vm("a", "TERMINATED")], disks=[d], addresses=[a])
    [f] = check_instances(i, PRICES, Options(), NOW)
    assert f.monthly_cost == round(17.0 + 0.005 * 730, 2)
    assert f.details["staticIPs"] == ["203.0.113.9"]


def test_suspended_vm_uses_suspend_timestamp():
    v = vm("s", "SUSPENDED", lastSuspendedTimestamp="2025-11-01T00:00:00Z")
    [f] = check_instances(inv(instances=[v]), PRICES, Options(min_idle_days=0), NOW)
    assert f.age_days == 9 and "memory state" in f.reason


def test_recently_stopped_vm_is_skipped():
    v = vm("s", "TERMINATED", lastStopTimestamp="2025-11-09T00:00:00Z")
    assert check_instances(inv(instances=[v]), PRICES, Options(), NOW) == []


# ---------------------------------------------------------------- helpers

def test_run_checks_only_runs_loaded_and_selected_checks():
    i = inv(disks=[disk("a")], snapshots=[snap("s", "2020-01-01T00:00:00Z")])
    found = run_checks(i, PRICES, Options(checks=("snapshots", "addresses")), NOW)
    assert [f.check for f in found] == ["snapshots"]


@pytest.mark.parametrize("text", [
    "2025-01-02T03:04:05.123-08:00",
    "2025-01-02T03:04:05Z",
    "2025-01-02T03:04:05.1234567+00:00",
    "2025-01-02T03:04:05",
])
def test_parse_ts_variants(text):
    assert parse_ts(text).tzinfo is not None


def test_parse_ts_rejects_garbage():
    assert parse_ts("yesterday") is None and parse_ts(None) is None


def test_resource_key_matches_full_and_partial_urls():
    assert resource_key(f"{B}/zones/z/disks/d") == resource_key("projects/demo-1/zones/z/disks/d")
