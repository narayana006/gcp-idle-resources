import json
from pathlib import Path

import pytest

from gcp_idle_resources.cli import main
from gcp_idle_resources.pricing import load_prices
from gcp_idle_resources.inventory import InputError

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
DIR = EXAMPLES / "acme-prod"
NOW = ["--now", "2025-11-10T00:00:00Z"]


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def test_example_table(capsys):
    code, out, _ = run(capsys, "--dir", str(DIR), *NOW)
    assert code == 0
    assert "Unattached disks: 3" in out
    assert "old-reports-export" in out and "tmp-restore" not in out  # tmp-restore detached 3 days ago
    assert "Unused static IP addresses: 2" in out and "db-vip" not in out
    assert "web-1-us-central1-a-20251109" not in out
    assert "batch-worker-old" in out and "legacy-ci" not in out
    assert "Total: 10 finding(s), estimated $189.99/month" in out


def test_example_json_totals(capsys):
    code, out, _ = run(capsys, "--dir", str(DIR), *NOW, "--format", "json")
    doc = json.loads(out)
    assert code == 0
    assert doc["totals"]["count"] == 10
    assert doc["totals"]["by_check"]["instances"]["count"] == 2
    assert doc["checked"] == ["disks", "addresses", "snapshots", "instances"]


def test_commands_format_is_never_executed_and_lists_deletes(capsys):
    code, out, _ = run(capsys, "--dir", str(DIR), *NOW, "--format", "commands")
    assert out.startswith("# Cleanup commands")
    assert "gcloud compute addresses delete promo-2025-ip --global --project=acme-prod-1234" in out


def test_single_file_and_checks_subset(capsys):
    code, out, _ = run(capsys, "--snapshots", str(DIR / "snapshots.json"), *NOW, "--snapshot-age-days", "300")
    assert "Old snapshots: 2" in out
    assert "Not checked (no input file): disks, addresses, instances" in out


def test_fail_on_findings_exit_code(capsys):
    assert run(capsys, "--dir", str(DIR), *NOW, "--fail-on-findings")[0] == 1


def test_no_findings_with_fail_flag_exits_zero(tmp_path, capsys):
    (tmp_path / "disks.json").write_text("[]")
    code, out, _ = run(capsys, "--dir", str(tmp_path), "--fail-on-findings")
    assert code == 0 and "Unattached disks: 0" in out


def test_empty_file_is_treated_as_empty_list(tmp_path, capsys):
    (tmp_path / "addresses.json").write_text("")
    assert run(capsys, "--addresses", str(tmp_path / "addresses.json"))[0] == 0


def test_bad_json_exits_2(tmp_path, capsys):
    p = tmp_path / "disks.json"
    p.write_text("{not json")
    code, _, err = run(capsys, "--disks", str(p))
    assert code == 2 and "not valid JSON" in err


def test_non_list_json_exits_2(tmp_path, capsys):
    p = tmp_path / "disks.json"
    p.write_text('"hello"')
    assert run(capsys, "--disks", str(p))[0] == 2


def test_describe_object_and_items_page_are_accepted(tmp_path, capsys):
    p = tmp_path / "d.json"
    p.write_text(json.dumps({"items": [{"name": "x", "sizeGb": "1", "zone": "zones/z"}]}))
    code, out, _ = run(capsys, "--disks", str(p), "--min-idle-days", "0")
    assert code == 0 and "Unattached disks: 1" in out


def test_no_inputs_is_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_bad_checks_value(capsys):
    with pytest.raises(SystemExit):
        main(["--dir", str(DIR), "--checks", "disks,vms"])


def test_bad_now(capsys):
    with pytest.raises(SystemExit):
        main(["--dir", str(DIR), "--now", "tomorrow"])


def test_empty_dir_exits_2(tmp_path, capsys):
    assert run(capsys, "--dir", str(tmp_path))[0] == 2


def test_price_override(capsys):
    code, out, _ = run(capsys, "--disks", str(DIR / "disks.json"), *NOW,
                       "--prices", str(EXAMPLES / "prices-override.json"))
    assert "$22.00" in out  # 500 GB pd-standard at 0.044


def test_price_file_rejects_unknown_key(tmp_path):
    p = tmp_path / "p.json"
    p.write_text('{"disk_gb": 1}')
    with pytest.raises(InputError):
        load_prices(p)


def test_defaults_not_mutated_by_override(tmp_path):
    p = tmp_path / "p.json"
    p.write_text('{"disk_gib_month": {"pd-ssd": 1}}')
    assert load_prices(p)["disk_gib_month"]["pd-ssd"] == 1
    assert load_prices(None)["disk_gib_month"]["pd-ssd"] == 0.17
