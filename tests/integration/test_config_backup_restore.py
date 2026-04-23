"""Config backup + restore round-trip validation.

Polls a device's config, then restores that same config back.
This validates the full pipeline without changing device behavior.
"""

import json

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.dev_blocking]


def test_backup_and_restore_roundtrip(session, config_ap):
    """Poll config → restore same config → verify device still matches."""
    ip = config_ap["ip"]

    # Step 1: Poll config to get a fresh snapshot
    poll_resp = session.post(f"/api/configs/{ip}/poll")
    assert poll_resp.status_code == 200
    original_hash = poll_resp.json()["config_hash"]

    # Step 2: Get the latest snapshot
    latest_resp = session.get(f"/api/configs/{ip}/latest")
    assert latest_resp.status_code == 200
    snapshot = latest_resp.json()
    config_id = snapshot["id"]

    # Step 3: Rollback to the same snapshot (no-op restore)
    rollback_resp = session.post(
        f"/api/config-push/rollback/{ip}",
        json={"config_id": config_id},
    )
    assert rollback_resp.status_code == 200, (
        f"Rollback failed: {rollback_resp.status_code} {rollback_resp.text[:300]}"
    )

    # Step 4: Poll again and verify config hash matches
    poll2_resp = session.post(f"/api/configs/{ip}/poll")
    assert poll2_resp.status_code == 200
    restored_hash = poll2_resp.json()["config_hash"]

    assert restored_hash == original_hash, (
        f"Config hash mismatch after restore: {original_hash} → {restored_hash}"
    )
