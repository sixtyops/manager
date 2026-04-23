"""Manager-level backup export tests."""

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.dev_blocking]

BACKUP_PASSPHRASE = "integration-test-passphrase"


def test_backup_export_returns_csv(session):
    """Export a manager backup and verify it returns CSV data."""
    resp = session.post("/api/backup/export", json={
        "passphrase": BACKUP_PASSPHRASE,
    })
    assert resp.status_code == 200
    assert "csv" in resp.headers.get("content-type", "").lower() or \
           "text" in resp.headers.get("content-type", "").lower()

    content = resp.text
    assert len(content) > 0, "Backup export is empty"
    # CSV should have a header row at minimum
    lines = content.strip().split("\n")
    assert len(lines) >= 1, "Backup CSV has no header"


def test_backup_export_contains_devices(session, topology):
    """Exported backup should contain at least the devices we know about."""
    resp = session.post("/api/backup/export", json={
        "passphrase": BACKUP_PASSPHRASE,
    })
    assert resp.status_code == 200

    content = resp.text
    # Check that at least one device IP appears in the CSV
    all_ips = []
    for site in topology["sites"]:
        for ap in site.get("aps", []):
            all_ips.append(ap["ip"])
        for sw in site.get("switches", []):
            all_ips.append(sw["ip"])

    found = any(ip in content for ip in all_ips)
    assert found, f"None of the known device IPs found in backup CSV"
