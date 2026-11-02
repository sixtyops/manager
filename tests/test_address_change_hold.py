"""Keep unqualified address transitions behind the shared firmware gate."""

from datetime import datetime, timedelta, timezone
import json
import hashlib
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from updater import app as app_module, database as db, rollout_gate as gate
from updater.vendors.base import VendorDeviceInfo, VendorUpdateResult, VendorSmokeTestResult
from updater.vendors.tachyon.client import DeviceInfo, TachyonClient

IP = "192.0.2.10"
OTHER_IP = "192.0.2.11"
SOURCE = "1.12.3.54970"
TARGET = "1.15.0.55142"
FW = "tna-30x-1.15.0-r55142-test.bin"
ARTIFACT = b"synthetic qualified firmware"
SCOPE = {
    "target_sha256": hashlib.sha256(ARTIFACT).hexdigest(),
    "management_vlan": 100,
    "management_ip_mode": "dhcp",
    "recovery_case": "firmware_flash",
}
OBSERVED = {"management_vlan": 100, "management_ip_mode": "dhcp"}
TRANSITION = gate.AddressTransition("tna-30x", "ap", SOURCE, TARGET, **SCOPE)


def qualify():
    for recovery_case in ("pre_update_reboot", "firmware_flash"):
        gate.ADDRESS_CHANGE_QUALIFICATIONS[replace(TRANSITION, recovery_case=recovery_case)] = "synthetic proof"


@pytest.fixture
def tables(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "ADDRESS_CHANGE_QUALIFICATIONS", {})
    monkeypatch.setenv("SIXTYOPS_ADDRESS_BENCH_PLAN_FILE", str(tmp_path / "bench.json"))


def test_shipped_tables_start_empty():
    assert gate.ADDRESS_CHANGE_QUALIFICATIONS == {}
    assert gate._address_bench_plan("unregistered-plan") is None


@pytest.mark.parametrize("family,role,source,target", [
    ("tna-303l", "ap", SOURCE, TARGET), ("tna-30x", "cpe", SOURCE, TARGET),
    ("tna-30x", "ap", "1.12.3.54971", TARGET),
    ("tna-30x", "ap", SOURCE, "1.15.0.55143"),
    (None, "ap", SOURCE, TARGET), ("tna-30x", "unknown", SOURCE, TARGET),
    ("tna-30x", "ap", "1.12.3", TARGET), ("tna-30x", "ap", SOURCE, "1.15.0"),
    ("tna-30x", "ap", SOURCE + ".extra", TARGET),
])
def test_qualification_matches_every_key(tables, family, role, source, target):
    gate.ADDRESS_CHANGE_QUALIFICATIONS[TRANSITION] = "synthetic bench proof"
    allowed, reason = gate.address_change_decision(family, role, source, target, **SCOPE)
    assert not allowed
    assert "Address-change hold" in reason


def test_qualified_exact_build_proceeds(tables):
    gate.ADDRESS_CHANGE_QUALIFICATIONS[TRANSITION] = "synthetic bench proof"
    assert gate.address_change_decision("tna-30x", "ap", "1.12.3.r54970", TARGET, **SCOPE) == (True, None)
    gate.ADDRESS_CHANGE_QUALIFICATIONS[TRANSITION] = ""
    assert not gate.address_change_decision("tna-30x", "ap", SOURCE, TARGET, **SCOPE)[0]


@pytest.mark.parametrize("management_ip_mode", ["dhcp", "unknown"])
def test_management_ip_mode_does_not_bypass_address_change_hold(tables, management_ip_mode):
    allowed, reason = gate.address_change_decision(
        "tna-30x", "ap", SOURCE, TARGET, target_sha256=SCOPE["target_sha256"],
        management_vlan=100, management_ip_mode=management_ip_mode, recovery_case="firmware_flash",
    )
    assert not allowed
    assert "Address-change hold" in reason


def write_plan(**overrides):
    import os
    from pathlib import Path
    plan = {
        "name": "lab-plan", "approval": "synthetic approval",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "devices": [{"ip": IP, "family": "tna-30x", "role": "ap", "source": SOURCE, "target": TARGET,
                     **SCOPE, "recovery_case": recovery_case}
                    for recovery_case in ("pre_update_reboot", "firmware_flash")],
    }
    plan.update(overrides)
    Path(os.environ["SIXTYOPS_ADDRESS_BENCH_PLAN_FILE"]).write_text(json.dumps(plan))


@pytest.mark.parametrize("ip,name,scheduled,source,allowed", [
    (IP, "lab-plan", False, SOURCE, True),
    (OTHER_IP, "lab-plan", False, SOURCE, False),
    (IP, "", False, SOURCE, False), (IP, "other-plan", False, SOURCE, False),
    (IP, "lab-plan", True, SOURCE, False),
    (IP, "lab-plan", False, "1.12.3.54971", False),
])
def test_named_bench_exception_scope(tables, ip, name, scheduled, source, allowed):
    write_plan()
    assert gate.address_change_decision(
        "tna-30x", "ap", source, TARGET, ip=ip, bench_exception=name, scheduled=scheduled, **SCOPE,
    )[0] is allowed


def test_named_plan_rejects_legacy_source_hash_with_clear_hold(tables):
    write_plan()
    import os
    from pathlib import Path
    path = Path(os.environ["SIXTYOPS_ADDRESS_BENCH_PLAN_FILE"])
    plan = json.loads(path.read_text())
    plan["devices"][0]["source_sha256"] = "a" * 64
    path.write_text(json.dumps(plan))

    allowed, reason = gate.address_change_decision(
        "tna-30x", "ap", SOURCE, TARGET, ip=IP,
        bench_exception="lab-plan", **SCOPE,
    )

    assert not allowed
    assert "removed source_sha256 field" in reason


def test_mixed_named_plan_rejects_legacy_source_hash(tables):
    write_plan()
    import os
    from pathlib import Path
    path = Path(os.environ["SIXTYOPS_ADDRESS_BENCH_PLAN_FILE"])
    plan = json.loads(path.read_text())
    plan["devices"][1]["source_sha256"] = "a" * 64
    path.write_text(json.dumps(plan))

    with pytest.raises(ValueError, match="removed source_sha256 field"):
        gate._address_bench_plan("lab-plan")


@pytest.mark.parametrize("overrides", [
    {"approval": ""}, {"approval": " "}, {"expires_at": "2000-01-01T00:00:00+00:00"},
    {"expires_at": "2099-01-01T00:00:00"}, {"expires_at": "unknown"},
    {"devices": []}, {"devices": [{}]}, {"devices": "all"},
])
def test_invalid_or_expired_plan_holds(tables, overrides):
    write_plan(**overrides)
    assert not gate.address_change_decision(
        "tna-30x", "ap", SOURCE, TARGET, ip=IP, bench_exception="lab-plan", **SCOPE,
    )[0]


@pytest.mark.parametrize("contents", ["broken JSON", "[]", "{}", "x" * 65537])
def test_unreadable_plan_holds(tables, contents):
    import os
    from pathlib import Path
    Path(os.environ["SIXTYOPS_ADDRESS_BENCH_PLAN_FILE"]).write_text(contents)
    assert gate._address_bench_plan("lab-plan") is None




































SCOPE_CHANGES = [
    ("target_sha256", "b" * 64),
    ("target_sha256", ""), ("target_sha256", None),
    ("management_vlan", 101), ("management_vlan", None),
    ("management_vlan", True), ("management_vlan", -1), ("management_vlan", 4095),
    ("management_ip_mode", "static"), ("management_ip_mode", ""),
    ("management_ip_mode", "unknown"), ("management_ip_mode", None),
    ("recovery_case", "pre_update_reboot"), ("recovery_case", ""),
]


@pytest.mark.parametrize("bench", [False, True])
@pytest.mark.parametrize("field,value", SCOPE_CHANGES)
def test_changed_or_missing_scope_stays_held(tables, bench, field, value):
    if bench:
        write_plan()
    else:
        gate.ADDRESS_CHANGE_QUALIFICATIONS[TRANSITION] = "synthetic proof"
    scope = {**SCOPE, field: value}
    # The plan includes both cases; use an unknown case for this mismatch check.
    if bench and field == "recovery_case" and value == "pre_update_reboot":
        scope[field] = "unknown"
    assert not gate.address_change_decision(
        "tna-30x", "ap", SOURCE, TARGET, ip=IP,
        bench_exception="lab-plan" if bench else "", **scope,
    )[0]


@pytest.mark.parametrize("field", list(SCOPE))
def test_legacy_plan_without_scope_stays_held(tables, field):
    write_plan()
    import os
    from pathlib import Path
    path = Path(os.environ["SIXTYOPS_ADDRESS_BENCH_PLAN_FILE"])
    plan = json.loads(path.read_text())
    for device in plan["devices"]:
        device.pop(field)
    path.write_text(json.dumps(plan))
    assert gate._address_bench_plan("lab-plan") is None
