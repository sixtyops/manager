"""Synthetic config tests for Users templates on every merge path."""

import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from updater import app as app_module
from updater import database as db
from updater.config_utils import check_config_compliance, merge_template_config
from updater.poller import NetworkPoller


LIVE = {"system": {"users": [
    {"username": "manager-login", "password": "synthetic-login", "level": 15},
    {"username": "existing", "password": "$1$abcdefgh$" + "a" * 22, "level": 1,
     "enabled": True, "device_field": "keep"},
]}}
NEW_USER = {"username": "new-user", "password": "$1$ijklmnop$" + "b" * 22, "level": 1}


def fragment_for(case):
    user = {
        "add": NEW_USER,
        "change": {"username": "existing", "level": 15},
        "login": {"username": "manager-login"},
        "same": {"username": "existing", "password": "$1$qrstuvwx$" + "c" * 22,
                 "level": 1, "enabled": True},
    }[case]
    return {"system": {"users": [deepcopy(user)]}}


def assert_add_only(config, fragment):
    assert config["system"]["users"] == LIVE["system"]["users"] + [NEW_USER]
    assert check_config_compliance(config, [{"config_fragment": fragment}])


class TestUsersMerge:
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
        ("change", "users_existing_user_change.*existing"),
        ("login", "users_login_protected.*manager-login"),
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
        with pytest.raises(ValueError, match="users_existing_user_change.*existing"):
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
        with pytest.raises(ValueError, match="users_login_protected.*manager-login"):
            merge_template_config({}, fragment_for("login"), "manager-login")

    def test_empty_list_preserves_every_user(self):
        assert merge_template_config(LIVE, {"system": {"users": []}}, "manager-login") == LIVE

    def test_conflicting_templates_cannot_change_a_new_user(self):
        first = merge_template_config(LIVE, fragment_for("add"), "manager-login")
        with pytest.raises(ValueError, match="users_existing_user_change.*new-user"):
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


def fake_client():
    client = MagicMock()
    client.login = AsyncMock(return_value=True)
    client.connect = AsyncMock(return_value=True)
    client.get_config = AsyncMock(side_effect=lambda: deepcopy(LIVE))
    client.get_hardware_id.return_value = "synthetic-model"
    client.apply_config = AsyncMock(return_value={"success": True})
    return client


def check_apply(client, case):
    if case in ("change", "login"):
        client.apply_config.assert_not_awaited()
    else:
        assert client.apply_config.await_count == 2
        dry, apply = client.apply_config.await_args_list
        assert dry.kwargs == {"dry_run": True}
        assert apply.kwargs == {}
        assert dry.args == apply.args
        if case == "add":
            assert_add_only(apply.args[0], fragment_for(case))
        else:
            assert apply.args[0] == LIVE


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
        assert ("existing" if case == "change" else "manager-login") in response.json()["detail"]
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
@pytest.mark.parametrize("case", ["add", "same", "change", "login"])
async def test_immediate_push_users(users_db, case):
    client = fake_client()
    messages = AsyncMock()
    job = {"cancelled": False}
    device = {"ip": "192.0.2.1", "username": "manager-login",
              "password": "synthetic-login", "role": "ap"}
    with patch("updater.app.TachyonClient", return_value=client), \
         patch("updater.app.broadcast", messages), patch("updater.app.get_poller", return_value=None):
        await app_module._run_config_push("synthetic-job", [device], [
            {"id": 1, "name": "Users", "fragment": fragment_for(case)},
        ], job)
    check_apply(client, case)
    assert job["failed"] == (1 if case in ("change", "login") else 0)
    if job["failed"]:
        errors = [call.args[0].get("error", "") for call in messages.await_args_list]
        assert any(("users_existing_user_change" if case == "change" else "users_login_protected")
                   in error for error in errors)
    assert "192.0.2.1" not in app_module._config_pushing_ips


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["add", "same", "change", "login"])
async def test_rollout_resume_users(users_db, case):
    snapshot = [{"id": 1, "name": "Users", "fragment": fragment_for(case)}]
    cursor = users_db.execute(
        "INSERT INTO config_push_rollouts "
        "(template_ids, template_names, templates_snapshot, phase, status) VALUES (?, ?, ?, ?, ?)",
        ("[1]", "Users", json.dumps(snapshot), "canary", "paused"),
    )
    rid = cursor.lastrowid
    users_db.execute(
        "INSERT INTO config_push_rollout_devices "
        "(rollout_id, ip, device_type, phase_assigned, status) VALUES (?, ?, ?, ?, ?)",
        (rid, "192.0.2.1", "ap", "canary", "failed"),
    )
    users_db.commit()
    client = fake_client()
    with patch("updater.app.TachyonClient", return_value=client), \
         patch("updater.app.broadcast", AsyncMock()), patch("updater.app.get_poller", return_value=None):
        result = await app_module.resume_config_push_rollout(rid, session={"role": "operator"})
        assert result == {"status": "resumed"}
        await app_module._config_push_rollout_task
    check_apply(client, case)
    row = db.get_config_push_rollout_devices(rid)[0]
    assert row["status"] == ("failed" if case in ("change", "login") else "updated")
    if row["status"] == "failed":
        assert ("users_existing_user_change" if case == "change" else "users_login_protected") in row["error"]
        assert db.get_config_push_rollout(rid)["status"] == "paused"
    assert "192.0.2.1" not in app_module._config_pushing_ips


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["add", "same", "change", "login"])
async def test_auto_enforce_users(users_db, case):
    client = fake_client()
    with patch("updater.poller.get_driver", return_value=MagicMock(return_value=client)):
        result = await NetworkPoller()._enforce_device("192.0.2.1", "ap", [
            {"id": 1, "config_fragment": json.dumps(fragment_for(case))},
        ], "pct10", asyncio.Semaphore(1))
    check_apply(client, case)
    if case in ("change", "login"):
        assert result[0:2] == (False, "policy")
        assert ("users_existing_user_change" if case == "change" else "users_login_protected") in result[2]
    else:
        assert result[0:3] == (True, None, None)
