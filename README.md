# gcp-idle-resources

Find Google Cloud resources you pay for but do not use: unattached persistent disks, reserved static IPs nobody uses, old snapshots, and stopped or suspended VMs that still hold disks and static IPs. Each finding gets an estimated monthly cost.

It reads the JSON that `gcloud compute ... list --format=json` prints. It never calls Google Cloud itself, never deletes anything, and needs no credentials.

**Status: working first version (0.1.0).** All four checks below work on gcloud JSON exports, with table, JSON and "cleanup commands" output. The example data is hand-written to match the Compute Engine v1 resource fields; the tool has not yet been run by the author against exports from a real project, so check its findings before acting on them.

## Install

```bash
pip install git+https://github.com/narayana006/gcp-idle-resources
```

Python 3.10 or newer. No runtime dependencies.

## Quick start

`examples/acme-prod/` holds an illustrative, hand-written export of a fictional project `acme-prod-1234` (addresses are from the documentation ranges 203.0.113.0/24 and 198.51.100.0/24). `--now` pins the clock so the ages below are reproducible:

```console
$ gcp-idle-resources --dir examples/acme-prod --now 2025-11-10T00:00:00Z
Unattached disks: 3
  NAME                LOCATION            PER MONTH  WHY
  ha-db-replica       region/us-central1  $34.00     unattached pd-ssd disk, 100 GB, detached 80 days ago, regional (two replicas)
  migration-scratch   zone/us-central1-a  $34.00     pd-ssd disk, 200 GB, never attached (created 240 days ago)
  old-reports-export  zone/us-central1-a  $20.00     unattached pd-standard disk, 500 GB, detached 160 days ago

Unused static IP addresses: 2
  NAME            LOCATION            PER MONTH  WHY
  old-bastion-ip  region/us-central1  $7.30      static external IP 203.0.113.25 reserved but not used by anything
  promo-2025-ip   global              $7.30      static external IP 198.51.100.40 reserved but not used by anything

Old snapshots: 3
  NAME                   LOCATION  PER MONTH  WHY
  reports-pre-migration  global    $8.53      434 days old, 170.5 GiB stored, source disk 500 GB
  db-archive-q1          global    $1.71      222 days old, 90.0 GiB stored, source disk 100 GB, archive snapshot
  ci-cache-2024          global    $1.00      706 days old, 20.0 GiB stored, source disk 50 GB, source disk ci-cache no longer exists

Stopped or suspended VMs (still billed for disks and static IPs): 2
  NAME              LOCATION            PER MONTH  WHY
  batch-worker-old  zone/us-central1-a  $56.15     stopped 116 days ago; 2 persistent disk(s), 350 GB; static IP 203.0.113.77 still reserved
  gpu-experiment    zone/us-central1-a  $20.00     suspended 39 days ago; 1 persistent disk(s), 200 GB; preserved memory state is billed too (not estimated)

Total: 10 finding(s), estimated $189.99/month (example prices; see --prices)
Read-only: nothing was changed. Use --format commands to list cleanup commands to review.
```

Not listed, on purpose: disks attached to a VM, a disk detached 3 days ago (`tmp-restore`, under the 7-day default), addresses that are `IN_USE`, a reserved internal VIP (shown only with `--include-internal`), the private-services peering range (never shown), yesterday's scheduled snapshot, a running VM and a VM stopped 4 days ago.

## Use it on your project

Export the four lists (any subset works; checks without input are skipped and named in the output):

```bash
mkdir export
gcloud compute disks list --format=json     > export/disks.json
gcloud compute addresses list --format=json > export/addresses.json
gcloud compute snapshots list --format=json > export/snapshots.json
gcloud compute instances list --format=json > export/instances.json

gcp-idle-resources --dir export
```

Add `--project=PROJECT_ID` to each gcloud command to export a project other than your default. Files can also be passed one by one with `--disks`, `--addresses`, `--snapshots` and `--instances`.

| Option | Meaning |
| --- | --- |
| `--dir DIR` | Read `disks.json`, `addresses.json`, `snapshots.json`, `instances.json` from DIR (missing files are skipped). |
| `--disks/--addresses/--snapshots/--instances FILE` | Read one export; overrides the file found by `--dir`. |
| `--checks LIST` | Subset of `disks,addresses,snapshots,instances` (default: all). |
| `--min-idle-days N` | Skip disks detached, addresses created and VMs stopped fewer than N days ago (default 7). |
| `--snapshot-age-days N` | Report snapshots older than N days (default 90). |
| `--include-internal` | Also report unused reserved internal addresses (no cost estimate). |
| `--prices FILE` | Override the example prices (see below). |
| `--format table\|json\|commands` | Output format (default `table`). |
| `--now TIMESTAMP` | Compute ages as of this RFC3339 time. |
| `--fail-on-findings` | Exit 1 when anything is found, for scheduled jobs and CI. |

Exit codes: 0 success, 1 findings with `--fail-on-findings`, 2 bad arguments or unreadable input.

### Cleanup commands

`--format commands` prints the gcloud commands you could run after reviewing each finding. The tool only prints them:

```console
$ gcp-idle-resources --dir examples/acme-prod --now 2025-11-10T00:00:00Z --format commands --checks addresses,instances
# Cleanup commands generated by gcp-idle-resources. NOT executed.
# Review every line. Deletion is permanent; snapshot a disk first if you may need its data.

# Unused static IP addresses
gcloud compute addresses delete old-bastion-ip --region=us-central1 --project=acme-prod-1234
gcloud compute addresses delete promo-2025-ip --global --project=acme-prod-1234

# Stopped or suspended VMs (still billed for disks and static IPs)
# batch-worker-old: deleting it also deletes auto-delete disk(s) batch-worker-old
gcloud compute instances delete batch-worker-old --zone=us-central1-a --project=acme-prod-1234
# gpu-experiment: deleting it also deletes auto-delete disk(s) gpu-experiment
gcloud compute instances delete gpu-experiment --zone=us-central1-a --project=acme-prod-1234
```

### JSON

`--format json` returns `generated_at`, `checked`, `currency`, `totals` (overall and per check) and `findings`, each with `check`, `name`, `project`, `location`, `reason`, `monthly_cost`, `age_days`, `command` and check-specific `details`.

## How each check decides

| Check | Reported when | Fields used |
| --- | --- | --- |
| Unattached disks | `users` is empty; idle time from `lastDetachTimestamp`, else `creationTimestamp` | `sizeGb`, `type`, `zone`/`region`, `lastAttachTimestamp`, `lastDetachTimestamp` |
| Unused static IPs | `status` is `RESERVED` with no `users`; `addressType` `EXTERNAL` (internal only with `--include-internal`; `VPC_PEERING` and `PRIVATE_SERVICE_CONNECT` ranges never) | `address`, `addressType`, `ipVersion`, `purpose`, `region` |
| Old snapshots | `creationTimestamp` older than `--snapshot-age-days`; notes when `sourceDisk` is not in `disks.json` and when `autoCreated` is true | `storageBytes`, `diskSizeGb`, `snapshotType`, `sourceDisk` |
| Stopped VMs | `status` is `TERMINATED` (stopped) or `SUSPENDED`; idle time from `lastStopTimestamp` / `lastSuspendedTimestamp` | `disks[]` (persistent only), `networkInterfaces[].accessConfigs[].natIP` matched to `IN_USE` static addresses |

Disks attached to a stopped VM are costed under that VM, not as unattached disks, so nothing is counted twice. With `disks.json` present the VM's disks are priced by their real type; without it the size from the VM's `disks[].diskSizeGb` is priced at the `default` rate.

## Prices

All prices are **example defaults** in USD, roughly in line with public list prices for a US region. They are not exact, vary by region and ignore discounts and free tiers:

| Key | Default |
| --- | --- |
| `disk_gib_month` | `pd-standard` 0.04, `pd-balanced` 0.10, `pd-ssd` 0.17, `pd-extreme` 0.125, `default` 0.10 (any other type) |
| `regional_disk_multiplier` | 2.0 |
| `snapshot_gib_month` / `archive_snapshot_gib_month` | 0.05 / 0.019 per GiB of `storageBytes` |
| `static_ip_unused_hour` / `static_ip_in_use_hour` | 0.010 / 0.005 (IPv4) |

Override any of them with a JSON file containing only the keys you want to change; `examples/prices-override.json` shows the format (its numbers are illustrative too):

```bash
gcp-idle-resources --dir export --prices my-prices.json
```

## Permissions

The tool itself needs no permissions or credentials. The gcloud export commands need read access to Compute Engine, for example the `roles/compute.viewer` role, which includes `compute.disks.list`, `compute.addresses.list`, `compute.globalAddresses.list`, `compute.snapshots.list` and `compute.instances.list`.

## Limitations

- Costs are estimates from the price table, not from your bill. Hyperdisk provisioned IOPS and throughput, the memory state of suspended VMs, IPv6 addresses and internal addresses are not costed.
- Snapshot `storageBytes` reflects storage shared between snapshots of the same disk, so deleting one snapshot may save less than shown.
- Only Compute Engine disks, addresses, snapshots and VMs. Not images, machine images, instance templates, load balancers, Cloud SQL or GKE.
- One export at a time; to cover several projects, run once per project export.
- No live mode: it reads files, by design.

## Feature checklist

- [x] Unattached disks (zonal and regional) with idle time and cost
- [x] Reserved static IPs not in use (regional and global)
- [x] Old snapshots, with deleted-source-disk and schedule notes
- [x] Stopped and suspended VMs with the disks and static IPs they still hold
- [x] Configurable price table with labelled example defaults
- [x] Table, JSON and cleanup-command output; `--fail-on-findings` exit code
- [ ] Multi-project exports in one run
- [ ] Images and machine images
- [ ] Optional live read through the Compute API

## Contributing

Issues and pull requests are welcome. Run the tests with:

```bash
pip install -e ".[dev]"
pytest
```

A sanitised real `gcloud ... list --format=json` export that the tool misreads is the most useful bug report.

Built and maintained by [Narayana Vemburaj](https://github.com/narayana006).

Licensed under the [MIT License](LICENSE).
