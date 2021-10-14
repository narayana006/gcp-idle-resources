# gcp-idle-resources

Find Google Cloud resources you pay for but do not use: unattached disks, unused static IPs, old snapshots and stopped VMs with disks.

## Planned scope

Offline on gcloud JSON exports: `compute disks list` (no users), `compute addresses list` (status RESERVED, not IN_USE), `compute snapshots list` (age, size), `compute instances list` (TERMINATED with attached disks/IPs). Monthly cost estimates from a configurable price file with clearly labelled example defaults (do not claim exact prices). Table/JSON. Read-only; prints the gcloud commands a user could run to clean up, never runs them.
