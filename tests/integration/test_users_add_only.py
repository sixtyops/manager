"""Live config coverage for add-only Users templates and restore."""

from __future__ import annotations

import os
import subprocess
import time

import httpx
import pytest


def _users(config: dict) -> list[dict]:
    users = config.get("system", {}).get("users")
    if not isinstance(users, list):
        pytest.fail("Config does not contain a Users list")
    return users


def _same_users(actual: list[dict], expected: list[dict]) -> bool:
    return actual == expected


def _contains_user(users: list[dict], username: str) -> bool:
    return any(isinstance(user, dict) and user.get("username") == username for user in users)


def _has_one_user(users: list[dict], username: str) -> bool:
    return sum(
        isinstance(user, dict) and user.get("username") == username
        for user in users
    ) == 1


def _existing_users_unchanged(
    actual: list[dict], baseline: list[dict], test_username: str
) -> bool:
    return [user for user in actual if user.get("username") != test_username] == baseline


def _manager_user_unchanged(
    users: list[dict], username: str, baseline_entry: dict
) -> bool:
    matches = [user for user in users if user.get("username") == username]
    return len(matches) == 1 and matches[0] == baseline_entry


def _cleanup_action(push_attempted: bool, push_accepted: bool, push_stopped: bool) -> str:
    if not push_attempted:
        return "delete_template"
    if not push_accepted or not push_stopped:
        return "hold_for_operator"
    return "restore_baseline"


def _wait_for_config_push(session, job_id: str, timeout: int = 180) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = session.get(f"/api/config-push/jobs/{job_id}")
        if response.status_code != 200:
            raise RuntimeError("Could not confirm config push status")
        job = response.json()
        if job.get("done") is True:
            return job
        time.sleep(2)
    raise TimeoutError("Timed out waiting for config push completion")


def _cancel_and_confirm_stopped(
    session, job_id: str, attempts: int = 15, poll_interval: float = 2
) -> bool:
    try:
        session.post(f"/api/config-push/jobs/{job_id}/cancel")
    except Exception:
        pass

    for attempt in range(attempts):
        try:
            response = session.get(f"/api/config-push/jobs/{job_id}")
            if response.status_code == 200 and response.json().get("done") is True:
                return True
        except Exception:
            pass
        if attempt + 1 < attempts:
            time.sleep(poll_interval)
    return False


def _device_login(ip: str, username: str, password: str) -> bool:
    try:
        with httpx.Client(verify=False, timeout=30, trust_env=False) as client:
            response = client.post(
                f"https://{ip}/cgi.lua/login",
                json={"username": username, "password": password},
            )
            if response.status_code != 200 or not client.cookies.get("token"):
                return False
            try:
                data = response.json()
            except ValueError:
                return False
            return data.get("statusCode") != 401 and data.get("auth") is not False
    except httpx.HTTPError:
        return False


def _direct_device_config(ip: str, username: str, password: str) -> dict | None:
    try:
        with httpx.Client(verify=False, timeout=30, trust_env=False) as client:
            login = client.post(
                f"https://{ip}/cgi.lua/login",
                json={"username": username, "password": password},
            )
            if login.status_code != 200 or not client.cookies.get("token"):
                return None
            try:
                login_data = login.json()
            except ValueError:
                return None
            if login_data.get("statusCode") == 401 or login_data.get("auth") is False:
                return None

            response = client.get(f"https://{ip}/cgi.lua/config")
            if response.status_code != 200:
                return None
            config = response.json()
            return config if isinstance(config, dict) else None
    except (httpx.HTTPError, ValueError):
        return None


def _users_write_inputs(config_ap_ip: str) -> tuple[str, str]:
    required = {
        "SIXTYOPS_TEST_ALLOW_USERS_WRITE": "1",
        "SIXTYOPS_TEST_USERS_WRITE_AP_IP": config_ap_ip,
        "SIXTYOPS_TEST_USERS_WRITE_RECOVERY_CONFIRMED": "1",
        "SIXTYOPS_TEST_USERS_WRITE_WRITERS_ISOLATED": "1",
    }
    if any(os.environ.get(name, "").strip() != expected for name, expected in required.items()):
        pytest.skip("Users write approval, AP binding, recovery, or writer isolation is not confirmed")

    try:
        candidate = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("Could not verify the current test candidate")

    if os.environ.get("SIXTYOPS_TEST_USERS_WRITE_HEAD", "").strip() != candidate:
        pytest.skip("Users write approval does not match the current test candidate")
    if os.environ.get("SIXTYOPS_TEST_USERS_WRITE_MANAGER_HEAD", "").strip() != candidate:
        pytest.skip("The live Manager head does not match the approved test candidate")
    if os.environ.get("SIXTYOPS_TEST_USERS_WRITE_RECOVERY_METHOD", "").strip() not in {
        "separate_account", "physical_console",
    }:
        pytest.skip("A verified recovery method is not confirmed")

    username = os.environ.get("SIXTYOPS_TEST_USERS_WRITE_USERNAME", "").strip()
    password = os.environ.get("SIXTYOPS_TEST_USERS_WRITE_PASSWORD", "")
    if not username or not password:
        pytest.skip("Approved Users test account inputs are not set")
    return username, password


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self.payload = payload

    def json(self) -> dict:
        return self.payload


class _FakePushSession:
    def __init__(self, statuses):
        self.statuses = iter(statuses)
        self.cancel_calls = 0

    def post(self, _path: str):
        self.cancel_calls += 1
        return _FakeResponse(200, {"status": "cancelled"})

    def get(self, _path: str):
        status = next(self.statuses)
        if isinstance(status, Exception):
            raise status
        return _FakeResponse(200, {"done": status})


def test_users_write_skips_without_opt_in(monkeypatch):
    names = (
        "SIXTYOPS_TEST_ALLOW_USERS_WRITE",
        "SIXTYOPS_TEST_USERS_WRITE_AP_IP",
        "SIXTYOPS_TEST_USERS_WRITE_RECOVERY_CONFIRMED",
        "SIXTYOPS_TEST_USERS_WRITE_WRITERS_ISOLATED",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(pytest.skip.Exception):
        _users_write_inputs("192.0.2.10")


def test_cancel_response_does_not_replace_stopped_confirmation(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    session = _FakePushSession([RuntimeError("read failed"), True])

    assert _cancel_and_confirm_stopped(session, "synthetic-job", attempts=2, poll_interval=0)
    assert session.cancel_calls == 1


def test_cancel_ack_with_active_job_does_not_allow_restore(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    session = _FakePushSession([False, False])

    stopped = _cancel_and_confirm_stopped(session, "synthetic-job", attempts=2, poll_interval=0)

    assert not stopped
    assert _cleanup_action(True, True, stopped) == "hold_for_operator"


def test_active_or_unknown_push_holds_baseline_and_template():
    assert _cleanup_action(True, False, False) == "hold_for_operator"
    assert _cleanup_action(True, True, False) == "hold_for_operator"


def test_failure_before_push_does_not_restore_baseline():
    assert _cleanup_action(False, False, True) == "delete_template"


@pytest.mark.integration
@pytest.mark.dev_blocking
def test_users_template_add_only_and_restore(
    session, config_ap, portal_credentials, request_with_retry
):
    ip = config_ap["ip"]
    test_username, test_password = _users_write_inputs(ip)

    manager_username, manager_password = portal_credentials(ip)
    poll = request_with_retry("POST", f"/api/configs/{ip}/poll", timeout=90.0)
    assert poll.status_code == 200, "Could not capture the baseline config snapshot"
    baseline_hash = poll.json()["config_hash"]

    latest = session.get(f"/api/configs/{ip}/latest")
    assert latest.status_code == 200, "Could not read the baseline config snapshot"
    baseline = latest.json()
    baseline_id = baseline["id"]
    baseline_users = _users(baseline["config_json"])
    test_user_exists = _contains_user(baseline_users, test_username)
    assert not test_user_exists, "Approved test account already exists"

    manager_entry = next(
        (user for user in baseline_users if user.get("username") == manager_username), None
    )
    assert manager_entry is not None, "Manager login user is missing from the baseline"
    baseline_direct = _direct_device_config(ip, manager_username, manager_password)
    assert baseline_direct is not None, "Manager login could not read the device baseline"
    baseline_users_match = _same_users(_users(baseline_direct), baseline_users)
    assert baseline_users_match, "Direct baseline Users read did not match"

    template_id = None
    push_attempted = False
    push_accepted = False
    push_stopped = True
    push_job_id = None
    try:
        created = session.post("/api/config-templates", json={
            "name": f"__users_add_only_{os.urandom(8).hex()}",
            "category": "users",
            "config_fragment": {
                "system": {
                    "users": [{"username": test_username, "password": test_password, "level": 0}],
                },
            },
            "description": "Integration test Users add-only template",
        })
        assert created.status_code == 200, "Could not create the Users test template"
        template_id = created.json()["id"]

        preview = session.post("/api/config-push/preview", json={
            "ip": ip,
            "template_ids": [template_id],
        })
        assert preview.status_code == 200, "Users template preview failed"
        preview_data = preview.json()
        preview_users = _users(preview_data["merged"])
        preview_existing_users_match = _existing_users_unchanged(
            preview_users, baseline_users, test_username
        )
        assert preview_existing_users_match, "Preview changed an existing device user"
        preview_has_test_user = _has_one_user(preview_users, test_username)
        assert preview_has_test_user, "Preview did not add the approved test user"
        preview_manager_unchanged = _manager_user_unchanged(
            preview_users, manager_username, manager_entry
        )
        assert preview_manager_unchanged, "Preview changed the Manager login user"
        assert preview_data.get("changed") is True, "Users template preview did not report a change"

        push_attempted = True
        try:
            started = session.post("/api/config-push", json={
                "template_ids": [template_id],
                "targets": [{"type": "ap", "ip": ip}],
            })
        except Exception:
            raise RuntimeError("Config push state is unknown") from None

        if 400 <= started.status_code < 500:
            push_attempted = False
        assert started.status_code == 200, "Could not start the Users config push"
        try:
            push_job_id = started.json().get("job_id")
        except ValueError:
            raise RuntimeError("Config push state is unknown") from None
        if not isinstance(push_job_id, str) or not push_job_id:
            raise RuntimeError("Config push state is unknown")
        push_accepted = True
        push_stopped = False

        try:
            job = _wait_for_config_push(session, push_job_id)
            push_stopped = True
        except BaseException:
            push_stopped = _cancel_and_confirm_stopped(session, push_job_id)
            raise
        assert job.get("failed") == 0, "Users config push failed"
        assert job.get("success") == 1, "Users config push did not complete successfully"

        test_user_can_login = _device_login(ip, test_username, test_password)
        assert test_user_can_login, "Added test user could not log in to the AP"
        direct_after_add = _direct_device_config(ip, manager_username, manager_password)
        assert direct_after_add is not None, "Manager login could not read the device after add"
        direct_users = _users(direct_after_add)
        direct_existing_users_match = _existing_users_unchanged(
            direct_users, baseline_users, test_username
        )
        assert direct_existing_users_match, "Direct read found a changed existing device user after add"
        direct_has_test_user = _has_one_user(direct_users, test_username)
        assert direct_has_test_user, "Direct read did not find the added test user"
        direct_manager_unchanged = _manager_user_unchanged(
            direct_users, manager_username, manager_entry
        )
        assert direct_manager_unchanged, "Direct read found a changed Manager login user after add"

        readback = request_with_retry("POST", f"/api/configs/{ip}/poll", timeout=90.0)
        assert readback.status_code == 200, "Manager config poll failed after add"
        fetched = session.get(f"/api/configs/{ip}/latest")
        assert fetched.status_code == 200, "Could not read the Manager snapshot after add"
        actual_users = _users(fetched.json()["config_json"])
        manager_poll_users_match = _existing_users_unchanged(
            actual_users, baseline_users, test_username
        )
        assert manager_poll_users_match, "Manager poll found a changed existing device user after add"
        manager_poll_has_test_user = _has_one_user(actual_users, test_username)
        assert manager_poll_has_test_user, "Manager poll did not find the added test user"
        manager_poll_login_unchanged = _manager_user_unchanged(
            actual_users, manager_username, manager_entry
        )
        assert manager_poll_login_unchanged, "Manager poll found a changed Manager login user after add"
    finally:
        action = _cleanup_action(push_attempted, push_accepted, push_stopped)
        if action == "hold_for_operator":
            pytest.fail("Config push may still be active or its state is unknown; keep the baseline for operator recovery")
        try:
            if action == "restore_baseline":
                restored = session.post(
                    f"/api/config-push/rollback/{ip}", json={"config_id": baseline_id}
                )
                assert restored.status_code == 200, "Baseline rollback failed"

                direct_restored = _direct_device_config(ip, manager_username, manager_password)
                assert direct_restored is not None, "Manager login could not read the device after restore"
                direct_restored_users = _users(direct_restored)
                direct_restore_users_match = _same_users(direct_restored_users, baseline_users)
                assert direct_restore_users_match, "Direct read found a Users mismatch after restore"
                direct_restore_has_test_user = _contains_user(direct_restored_users, test_username)
                assert not direct_restore_has_test_user, "Direct read found the added test user after restore"
                direct_restore_manager_unchanged = _manager_user_unchanged(
                    direct_restored_users, manager_username, manager_entry
                )
                assert direct_restore_manager_unchanged, "Direct read found a changed Manager login user after restore"

                verify = request_with_retry("POST", f"/api/configs/{ip}/poll", timeout=90.0)
                assert verify.status_code == 200, "Manager config poll failed after restore"
                assert verify.json().get("config_hash") == baseline_hash, "Config hash did not match baseline after restore"
                restored_snapshot = session.get(f"/api/configs/{ip}/latest")
                assert restored_snapshot.status_code == 200, "Could not read the restored Manager snapshot"
                restored_users = _users(restored_snapshot.json()["config_json"])
                manager_restore_users_match = _same_users(restored_users, baseline_users)
                assert manager_restore_users_match, "Manager poll found a Users mismatch after restore"
                manager_restore_has_test_user = _contains_user(restored_users, test_username)
                assert not manager_restore_has_test_user, "Manager poll found the added test user after restore"
                manager_restore_login_unchanged = _manager_user_unchanged(
                    restored_users, manager_username, manager_entry
                )
                assert manager_restore_login_unchanged, "Manager poll found a changed Manager login user after restore"
        finally:
            if template_id is not None and action != "hold_for_operator":
                deleted = session.delete(f"/api/config-templates/{template_id}")
                assert deleted.status_code == 200, "Could not remove the Users test template"
