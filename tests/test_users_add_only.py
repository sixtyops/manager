"""Synthetic config tests for Users templates on every merge path."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from updater import app as app_module
from updater import database as db
from updater.config_utils import check_config_compliance, merge_template_config
from updater.poller import NetworkPoller
from updater.vendors.tachyon import TachyonDriver
from updater.vendors.tachyon.client import TachyonClient


LIVE = {"system": {"users": [
    {"username": "manager-login", "password": "synthetic-login", "level": 15},
    {"username": "existing", "password": "$1$abcdefgh$" + "a" * 22, "level": 1,
     "enabled": True, "device_field": "keep"},
    {"username": "existing-plain", "password": "synthetic-existing", "level": 1},
    {"username": "existing-empty", "password": "", "level": 1},
    {"username": "existing-no-password", "level": 1},
]}}
NEW_USER = {"username": "new-user", "password": "$1$ijklmnop$" + "b" * 22, "level": 0}
NEW_DEVICE_USER = {
    "username": "new-user",
    "password": "$1$ijklmnop$" + "b" * 22,
    "level": 0,
    "enabled": True,
    "first_login": False,
}


def fragment_for(case):
    user = {
        "add": NEW_USER,
        "add_plain": {**NEW_USER, "password": "synthetic-new"},
        "change": {"username": "existing", "level": 15},
        "login": {"username": "manager-login"},
        "same": {"username": "existing", "password": "$1$qrstuvwx$" + "c" * 22,
                 "level": 1, "enabled": True},
    }[case]
    return {"system": {"users": [deepcopy(user)]}}


def assert_add_only(config, fragment):
    assert config["system"]["users"][:-1] == LIVE["system"]["users"]
    assert config["system"]["users"][-1] == NEW_DEVICE_USER
    assert check_config_compliance(config, [{"config_fragment": fragment}])


class TestUsersMerge:
    def test_only_added_plaintext_password_is_hashed(self):
        from passlib.hash import md5_crypt
        fragment = fragment_for("add_plain")
        original = deepcopy(fragment)
        merged = merge_template_config(LIVE, fragment, "manager-login")
        assert merged["system"]["users"][:-1] == LIVE["system"]["users"]
        added = merged["system"]["users"][-1]
        assert set(added) == {"username", "password", "level", "enabled", "first_login"}
        assert added["level"] == 0 and added["enabled"] is True
        assert added["first_login"] is False
        assert len(added["password"]) == 34 and added["password"].startswith("$1$")
        assert md5_crypt.verify("synthetic-new", added["password"])
        assert "synthetic-new" not in json.dumps(added)
        assert fragment == original

    def test_read_only_role_maps_to_device_level(self):
        from passlib.hash import md5_crypt
        fragment = {"system": {"users": [{
            "username": "read-only-user",
            "password": "synthetic-read-only",
            "level": 9,
        }]}}
        merged = merge_template_config(LIVE, fragment, "manager-login")
        added = merged["system"]["users"][-1]
        assert set(added) == {"username", "password", "level", "enabled", "first_login"}
        assert added["username"] == "read-only-user"
        assert added["level"] == 9 and added["enabled"] is True
        assert added["first_login"] is False
        assert md5_crypt.verify("synthetic-read-only", added["password"])
        assert merged["system"]["users"][:-1] == LIVE["system"]["users"]

    def test_add_preserves_users_inputs_and_compliance(self):
        original = deepcopy(LIVE)
        fragment = fragment_for("add")
        merged = merge_template_config(original, fragment, "manager-login")
        assert_add_only(merged, fragment)
        assert original == LIVE
        assert fragment == fragment_for("add")
        merged["system"]["users"][0]["level"] = 0
        assert original == LIVE

    def test_compatible_entry_keeps_device_record_and_hash(self):
        assert merge_template_config(LIVE, fragment_for("same"), "manager-login") == LIVE

    @pytest.mark.parametrize("case, error", [
        ("change", "users_existing_user_change"),
        ("login", "users_login_protected"),
    ])
    def test_named_rejection_keeps_inputs(self, case, error):
        original = deepcopy(LIVE)
        with pytest.raises(ValueError, match=error):
            merge_template_config(original, fragment_for(case), "manager-login")
        assert original == LIVE

    @pytest.mark.parametrize("fields", [
        {"password": "different"}, {"enabled": False}, {"first_login": True},
        {"level": 15}, {"device_field": "replace"},
    ])
    def test_each_changed_field_is_rejected(self, fields):
        fragment = {"system": {"users": [{"username": "existing", **fields}]}}
        with pytest.raises(ValueError, match="users_existing_user_change"):
            merge_template_config(LIVE, fragment, "manager-login")

    def test_other_lists_still_replace(self):
        base = {**LIVE, "services": {"ntp": {"servers": ["old"]}}, "users": [1]}
        fragment = {**fragment_for("add"), "services": {"ntp": {"servers": ["new"]}},
                    "users": [2]}
        merged = merge_template_config(base, fragment, "manager-login")
        assert_add_only(merged, fragment_for("add"))
        assert merged["services"]["ntp"]["servers"] == ["new"]
        assert merged["users"] == [2]

    @pytest.mark.parametrize("users", [None, {}, "bad", [None], [{}],
        [{"username": ""}], [NEW_USER, NEW_USER]])
    def test_malformed_template_users_fail_closed(self, users):
        with pytest.raises(ValueError, match="users_invalid"):
            merge_template_config(LIVE, {"system": {"users": users}}, "manager-login")

    @pytest.mark.parametrize("users", [None, {}, [None], [{}], [NEW_USER, NEW_USER]])
    def test_malformed_live_users_fail_closed(self, users):
        with pytest.raises(ValueError, match="users_invalid"):
            merge_template_config({"system": {"users": users}}, fragment_for("add"), "manager-login")

    @pytest.mark.parametrize("system", [None, [], "bad"])
    def test_parent_replacement_cannot_erase_users(self, system):
        with pytest.raises(ValueError, match="users_invalid"):
            merge_template_config(LIVE, {"system": system}, "manager-login")

    def test_login_must_be_known_for_users(self):
        with pytest.raises(ValueError, match="users_login_unknown"):
            merge_template_config(LIVE, fragment_for("add"), None)
        assert merge_template_config(LIVE, {"services": {}}, None) == {**LIVE, "services": {}}

    def test_login_entry_is_rejected_even_when_missing_from_live_config(self):
        with pytest.raises(ValueError, match="users_login_protected"):
            merge_template_config({}, fragment_for("login"), "manager-login")

    def test_empty_list_preserves_every_user(self):
        assert merge_template_config(LIVE, {"system": {"users": []}}, "manager-login") == LIVE

    def test_conflicting_templates_cannot_change_a_new_user(self):
        first = merge_template_config(LIVE, fragment_for("add"), "manager-login")
        with pytest.raises(ValueError, match="users_existing_user_change"):
            merge_template_config(first, {"system": {"users": [
                {"username": "new-user", "level": 15},
            ]}}, "manager-login")


@pytest.fixture
def users_db(mock_db):
    mock_db.execute(
        "INSERT INTO access_points (ip, username, password, enabled) VALUES (?, ?, ?, 1)",
        ("192.0.2.1", "manager-login", "synthetic-login"),
    )
    db.save_device_config("192.0.2.1", json.dumps(LIVE), "synthetic-hash", None, None)
    mock_db.commit()
    return mock_db


@pytest.fixture
def transport():
    """Run the real client and vendor driver with synthetic transport replies."""
    captured = {"posts": [], "logins": 0, "failure_response": None, "config": LIVE}

    async def run_curl(args):
        endpoint = args[-1].removeprefix("https://192.0.2.1")
        method = args[args.index("-X") + 1]
        if endpoint.endswith("/cgi.lua/login"):
            captured["logins"] += 1
            cookie_path = Path(args[args.index("-c") + 1])
            cookie_path.write_text("device.test\tFALSE\t/\tFALSE\t0\ttoken\tsynthetic-token\n")
            return 0, b'{"auth": true}'
        if endpoint == "/cgi.lua/config" and method == "GET":
            return 0, json.dumps(captured["config"]).encode() + b"\n200"
        assert endpoint in ("/cgi.lua/config?dry_run=true", "/cgi.lua/config")
        assert method == "POST"
        assert "-d" in args
        payload = json.loads(args[args.index("-d") + 1])
        captured["posts"].append((endpoint, payload))
        failure = captured["failure_response"]
        if failure and endpoint.endswith("?dry_run=true") == (failure[0] == "dry-run"):
            _stage, status, body = failure
            return 0, body.encode() + f"\n{status}".encode()
        return 0, b'{"success": true}\n200'

    with patch("updater.config_write_guard.automatic_config_writes_allowed", return_value=True), \
         patch("updater.vendors.tachyon.client._run_curl", side_effect=run_curl):
        yield captured


def check_apply(transport, case):
    posts = transport["posts"]
    if case in ("change", "login"):
        assert posts == []
    else:
        assert len(posts) == 2
        assert [endpoint for endpoint, _ in posts] == [
            "/cgi.lua/config?dry_run=true", "/cgi.lua/config",
        ]
        dry, apply = [payload for _, payload in posts]
        assert dry == apply
        for payload in (dry, apply):
            assert payload["system"]["users"][:len(LIVE["system"]["users"])] == LIVE["system"]["users"]
        if case == "add":
            assert_add_only(apply, fragment_for(case))
        elif case == "add_plain":
            from passlib.hash import md5_crypt
            added = apply["system"]["users"][-1]
            assert set(added) == {"username", "password", "level", "enabled", "first_login"}
            assert added["username"] == "new-user"
            assert added["level"] == 0 and added["enabled"] is True
            assert added["first_login"] is False
            assert len(added["password"]) == 34 and added["password"].startswith("$1$")
            assert md5_crypt.verify("synthetic-new", added["password"])
            assert "synthetic-new" not in json.dumps(added)
        else:
            assert apply == LIVE


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", [TachyonClient, TachyonDriver])
async def test_unrelated_apply_keeps_default_password_normalization(transport, driver):
    from passlib.hash import md5_crypt
    client = driver("192.0.2.1", "manager-login", "synthetic-login")
    config = deepcopy(LIVE)
    assert (await client.apply_config(config, dry_run=True))["success"]
    assert (await client.apply_config(config))["success"]
    posts = transport["posts"]
    assert len(posts) == 2
    assert posts[0][1] == posts[1][1]
    for _, payload in posts:
        users = payload["system"]["users"]
        assert md5_crypt.verify("synthetic-login", users[0]["password"])
        assert md5_crypt.verify("synthetic-existing", users[2]["password"])
        assert users[1] == LIVE["system"]["users"][1]
        assert users[3:] == LIVE["system"]["users"][3:]


@pytest.mark.parametrize("case", ["add", "same", "change", "login"])
def test_preview_users(operator_client, users_db, case):
    tid = db.save_config_template(name="Users", category="users",
                                  config_fragment=json.dumps(fragment_for(case)))
    with patch("updater.app.TachyonClient") as driver:
        response = operator_client.post("/api/config-push/preview", json={
            "ip": "192.0.2.1", "template_ids": [tid],
        })
    driver.assert_not_called()
    if case in ("change", "login"):
        assert response.status_code == 400, response.text
        code = "users_existing_user_change" if case == "change" else "users_login_protected"
        assert code in response.json()["detail"]
    else:
        assert response.status_code == 200, response.text
        if case == "add":
            assert_add_only(response.json()["merged"], fragment_for(case))
        else:
            assert response.json()["merged"] == LIVE
    assert json.loads(db.get_latest_device_config("192.0.2.1")["config_json"]) == LIVE


@pytest.mark.parametrize("role", ["switch", "cpe", "unknown"])
def test_preview_resolves_login_or_fails_closed(operator_client, users_db, role):
    ip = "192.0.2.2"
    if role == "switch":
        users_db.execute("INSERT INTO switches (ip, username, password) VALUES (?, ?, ?)",
                         (ip, "manager-login", "synthetic-login"))
    elif role == "cpe":
        users_db.execute("INSERT INTO cpe_cache (ap_ip, ip, mac) VALUES (?, ?, ?)",
                         ("192.0.2.1", ip, "00:00:5E:00:53:01"))
    users_db.commit()
    db.save_device_config(ip, json.dumps(LIVE), "synthetic-hash", None, None)
    tid = db.save_config_template(name="Users", category="users",
                                  config_fragment=json.dumps(fragment_for("login")))
    response = operator_client.post("/api/config-push/preview", json={
        "ip": ip, "template_ids": [tid],
    })
    assert response.status_code == 400, response.text
    assert ("users_login_unknown" if role == "unknown" else "users_login_protected") in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["add", "add_plain", "same", "change", "login"])
async def test_immediate_push_users(users_db, transport, case):
    messages = AsyncMock()
    job = {"cancelled": False}
    device = {"ip": "192.0.2.1", "username": "manager-login",
              "password": "synthetic-login", "role": "ap"}
    with patch("updater.app.broadcast", messages), patch("updater.app.get_poller", return_value=None):
        await app_module._run_config_push("synthetic-job", [device], [
            {"id": 1, "name": "Users", "fragment": fragment_for(case)},
        ], job)
    assert transport["logins"] == 1
    check_apply(transport, case)
    assert job["failed"] == (1 if case in ("change", "login") else 0)
    if job["failed"]:
        errors = [call.args[0].get("error", "") for call in messages.await_args_list]
        assert any(("users_existing_user_change" if case == "change" else "users_login_protected")
                   in error for error in errors)
    assert "192.0.2.1" not in app_module._config_pushing_ips


@pytest.mark.asyncio
@pytest.mark.parametrize("case, username", [("change", "existing"), ("login", "manager-login")])
async def test_rejected_template_username_is_absent_from_stored_and_broadcast_errors(
    users_db, transport, case, username
):
    if case == "change":
        username = "private-account-492"
        live = deepcopy(LIVE)
        live["system"]["users"].append({
            "username": username,
            "password": "$1$abcdefgh$" + "d" * 22,
            "level": 9,
            "enabled": True,
            "first_login": False,
        })
        fragment = {"system": {"users": [{"username": username, "level": 0}]}}
    else:
        live = LIVE
        fragment = fragment_for(case)
    db.save_device_config("192.0.2.1", json.dumps(live), "synthetic-hash", None, None)
    users_db.commit()
    transport["config"] = live

    messages = AsyncMock()
    job = {"cancelled": False}
    device = {"ip": "192.0.2.1", "username": "manager-login",
              "password": "synthetic-login", "role": "ap"}
    with patch("updater.app.broadcast", messages), patch("updater.app.get_poller", return_value=None):
        await app_module._run_config_push("privacy-job", [device], [
            {"id": 1, "name": "Users", "fragment": fragment},
        ], job)

    code = "users_existing_user_change" if case == "change" else "users_login_protected"
    stored_errors = [job["results"][0]["error"]]
    history, total = db.get_device_update_history(action="config_push")
    assert total == 1
    stored_errors.append(history[0]["error"])
    broadcast_errors = [
        call.args[0]["error"] for call in messages.await_args_list
        if "error" in call.args[0]
    ]
    assert len(broadcast_errors) == 1
    assert all(code in error for error in stored_errors + broadcast_errors)
    assert all(username not in error for error in stored_errors + broadcast_errors)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["dry-run", "apply"])
async def test_immediate_push_records_sanitized_device_rejection(users_db, transport, stage):
    transport["failure_response"] = (stage, 400, json.dumps({
        "error": "Invalid username=manager-login password=synthetic-login at 192.0.2.44",
    }))
    messages = AsyncMock()
    job = {"cancelled": False}
    device = {"ip": "192.0.2.1", "username": "manager-login",
              "password": "synthetic-login", "role": "ap"}
    with patch("updater.app.broadcast", messages), patch("updater.app.get_poller", return_value=None):
        await app_module._run_config_push("dry-run-failure", [device], [
            {"id": 1, "name": "Users", "fragment": fragment_for("add")},
        ], job)
    error_prefix = "Dry run rejected: " if stage == "dry-run" else ""
    expected_error = error_prefix + (
        "Configuration request failed (HTTP 400): "
        "Invalid username=[redacted] password=[redacted] at [address]"
    )
    assert job["results"] == [{
        "ip": "192.0.2.1", "status": "failed", "error": expected_error,
        "failed_stage": stage,
    }]
    history, total = db.get_device_update_history(action="config_push")
    assert total == 1
    assert history[0]["error"] == expected_error
    assert history[0]["failed_stage"] == stage
    assert all("synthetic-login" not in str(call.args) for call in messages.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["add", "add_plain", "same", "change", "login"])
async def test_auto_enforce_users(users_db, transport, case):
    result = await NetworkPoller()._enforce_device("192.0.2.1", "ap", [
        {"id": 1, "config_fragment": json.dumps(fragment_for(case))},
    ], "pct10", asyncio.Semaphore(1))
    assert transport["logins"] == 1
    check_apply(transport, case)
    if case in ("change", "login"):
        assert result[0:2] == (False, "policy")
        assert ("users_existing_user_change" if case == "change" else "users_login_protected") in result[2]
    else:
        assert result[0:3] == (True, None, None)
