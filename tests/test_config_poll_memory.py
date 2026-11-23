import asyncio
import gc
import hashlib
import json
import tracemalloc
from pathlib import Path

import pytest

from updater import crypto, database as db, logging_filter
from updater.poller import NetworkPoller
from updater.vendors import init_vendors
from updater.vendors.tachyon import TachyonDriver
from updater.vendors.tachyon.client import TachyonClient


@pytest.mark.asyncio
async def test_daily_config_poll_releases_fleet_configs(monkeypatch, mock_db):
    fixture_dir = Path(__file__).parent / "fixtures" / "tachyon"
    config = json.loads((fixture_dir / "management_zone_static.json").read_text())
    interfaces = json.loads((fixture_dir / "ap_interfaces.json").read_text())["interfaces"]
    config["network"]["interfaces"] = {
        name: {"enabled": True, "mtu": 1500, "macaddr": "02:00:00:00:00:02"}
        for name in interfaces
    }

    def device_config(ip):
        return json.dumps(
            {**config, "system": {"name": ip, "location": "Offline lab"}},
            sort_keys=True,
            separators=(",", ":"),
        )

    async def connect(self):
        return True

    async def curl(self, method, path):
        assert (method, path) == ("GET", "/cgi.lua/config")
        await asyncio.sleep(0)
        return 200, device_config(self.ip)

    init_vendors()
    monkeypatch.setattr(TachyonDriver, "connect", connect)
    monkeypatch.setattr(TachyonClient, "_curl", curl)
    monkeypatch.setattr(logging_filter, "_secrets", set())
    monkeypatch.setattr(logging_filter, "_device_tokens", {})
    monkeypatch.setattr(logging_filter, "_pattern", None)
    ips = [f"192.0.2.{i}" for i in range(1, 101)]
    for ip in ips:
        db.upsert_access_point(ip, "offline", "offline-fixture")
    registered_secrets = len(logging_filter._secrets)
    db.get_setting("config_poll_enabled")
    poller = NetworkPoller()

    gc.collect()
    tracemalloc.start()
    try:
        before, _ = tracemalloc.get_traced_memory()
        await poller.poll_all_configs()
        gc.collect()
        after, poll_peak = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        await poller._poll_missing_configs()
        _, priming_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    fleet_bytes = sum(len(device_config(ip)) for ip in ips)
    mib = 1024 ** 2
    print(
        f"Fleet JSON {fleet_bytes / mib:.3f} MiB, "
        f"poll retained {(after - before) / mib:.3f} MiB, "
        f"poll peak {(poll_peak - before) / mib:.3f} MiB, "
        f"next priming peak above post-poll baseline {(priming_peak - after) / mib:.3f} MiB"
    )
    assert after - before < fleet_bytes / 2
    assert priming_peak - after < fleet_bytes / 2
    rows = mock_db.execute("SELECT ip, config_json, config_hash FROM device_configs").fetchall()
    assert len(rows) == len(ips)
    for row in rows:
        expected = device_config(row["ip"])
        assert crypto.is_encrypted(row["config_json"])
        assert db.get_latest_device_config(row["ip"])["config_json"] == expected
        assert row["config_hash"] == hashlib.sha256(expected.encode()).hexdigest()
    assert len(logging_filter._secrets) == registered_secrets

    mock_db.execute("UPDATE device_configs SET deleted_at = CURRENT_TIMESTAMP WHERE ip = ?", (ips[0],))
    await poller._poll_missing_configs()
    live_rows = mock_db.execute("SELECT COUNT(*) FROM device_configs WHERE deleted_at IS NULL")
    assert live_rows.fetchone()[0] == len(ips)
    assert len(db.get_device_config_history(ips[0])) == 1
