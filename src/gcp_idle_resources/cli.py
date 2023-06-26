"""Command-line entry point: gcp-idle-resources."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .checks import CHECKS, Options, run_checks
from .inventory import InputError, find_in_dir, load_inventory, parse_ts
from .pricing import load_prices
from .report import render_commands, render_json, render_table

EPILOG = """\
inputs (all optional, at least one required):
  gcloud compute disks list --format=json > disks.json
  gcloud compute addresses list --format=json > addresses.json
  gcloud compute snapshots list --format=json > snapshots.json
  gcloud compute instances list --format=json > instances.json

exit codes: 0 ok, 1 findings with --fail-on-findings, 2 bad input or arguments
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gcp-idle-resources",
        description="Find unused Google Cloud disks, static IPs, old snapshots and stopped VMs "
                    "from gcloud JSON exports. Read-only.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--dir", type=Path, help="directory containing disks.json, addresses.json, "
                                            "snapshots.json and/or instances.json")
    p.add_argument("--disks", type=Path, help="output of gcloud compute disks list --format=json")
    p.add_argument("--addresses", type=Path, help="output of gcloud compute addresses list --format=json")
    p.add_argument("--snapshots", type=Path, help="output of gcloud compute snapshots list --format=json")
    p.add_argument("--instances", type=Path, help="output of gcloud compute instances list --format=json")
    p.add_argument("--checks", default=",".join(CHECKS),
                   help=f"comma-separated subset of {','.join(CHECKS)} (default: all)")
    p.add_argument("--min-idle-days", type=int, default=7,
                   help="ignore disks detached, addresses created and VMs stopped fewer than N days ago (default 7)")
    p.add_argument("--snapshot-age-days", type=int, default=90,
                   help="report snapshots older than N days (default 90)")
    p.add_argument("--include-internal", action="store_true",
                   help="also report unused reserved internal IP addresses (no cost estimate)")
    p.add_argument("--prices", type=Path, help="JSON file overriding the example default prices")
    p.add_argument("--format", choices=("table", "json", "commands"), default="table")
    p.add_argument("--now", help="evaluate ages as of this RFC3339 time instead of the current time")
    p.add_argument("--fail-on-findings", action="store_true", help="exit 1 when anything is found")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    checks = tuple(c.strip() for c in args.checks.split(",") if c.strip())
    bad = [c for c in checks if c not in CHECKS]
    if bad or not checks:
        parser.error(f"--checks accepts {','.join(CHECKS)}; got {args.checks!r}")
    if args.min_idle_days < 0 or args.snapshot_age_days < 0:
        parser.error("day thresholds must be zero or positive")

    if args.now:
        now = parse_ts(args.now)
        if now is None:
            parser.error(f"--now must be an RFC3339 timestamp such as 2025-11-10T00:00:00Z, got {args.now!r}")
    else:
        now = datetime.now(timezone.utc)

    try:
        paths = find_in_dir(args.dir) if args.dir else {}
        for kind in CHECKS:
            explicit = getattr(args, kind)
            if explicit is not None:
                paths[kind] = explicit
        if not any(paths.values()):
            parser.error("give --dir or at least one of --disks, --addresses, --snapshots, --instances")
        inv = load_inventory(paths)
        prices = load_prices(args.prices)
    except InputError as exc:
        print(f"gcp-idle-resources: error: {exc}", file=sys.stderr)
        return 2

    opts = Options(
        min_idle_days=args.min_idle_days,
        snapshot_age_days=args.snapshot_age_days,
        include_internal=args.include_internal,
        checks=checks,
    )
    findings = run_checks(inv, prices, opts, now)
    loaded = [c for c in CHECKS if c in checks and inv.has(c)]

    if args.format == "json":
        print(render_json(findings, loaded, prices, now.isoformat()))
    elif args.format == "commands":
        print(render_commands(findings))
    else:
        print(render_table(findings, loaded, prices))

    return 1 if findings and args.fail_on_findings else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
