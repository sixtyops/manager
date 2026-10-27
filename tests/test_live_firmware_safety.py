"""Exercise the live firmware test offline with a mocked Manager client."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location(
    "live_firmware", Path(__file__).parent / "integration" / "test_firmware.py")
firmware = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(firmware)

IP = "192.0.2.10"
ORIGINAL = "tna-30x-1.15.0-r100-sysupgrade.bin"
TARGET = "tna-30x-1.15.0-r200-sysupgrade.bin"
FILES = [{"name": name, "deployable": True} for name in (ORIGINAL, TARGET)]
BASELINE = {"ip": IP, "model": "TNA-301", "firmware_version": "1.15.0.100",
            "active_bank": 1, "bank1_version": "1.15.0.100", "bank2_version": "1.15.0.100"}


def response(data, status=200):
    return Mock(status_code=status, json=Mock(return_value=data))


@pytest.fixture
def client(monkeypatch):
    ticks = iter(range(10000))
    monkeypatch.setattr(firmware.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(firmware.time, "sleep", lambda _: None)
    monkeypatch.setattr(firmware.time, "time", lambda: 100)
    state = {"version": "1.15.0.100", "step": 0, "mode": "success"}
    session = Mock()

    def post(path, **kwargs):
        if path == "/api/update-device":
            state["step"] += 1
            name = kwargs["data"]["firmware_file"]
            state["version"] = firmware.extract_version_from_filename(name)
            if state["step"] == 1:
                if state["mode"] == "exception":
                    raise RuntimeError("PRIVATE_DEVICE_RESPONSE")
                if state["mode"] == "wrong-version":
                    state["version"] = "1.15.0.999"
            return response({"job_id": str(state["step"])})
        return response({"success": True})

    def get(path):
        if path == "/api/topology":
            ap = {**BASELINE, "firmware_version": state["version"],
                  "last_seen": "1970-01-01T00:01:41+00:00"}
            if state["mode"] == "stale":
                ap["last_seen"] = "1970-01-01T00:01:39+00:00"
            if state["mode"] == "bank-mismatch" and state["step"] == 2:
                ap["active_bank"] = 2
            return response({"sites": [{"switches": [{"aps": [ap]}]}]})
        failed = state["mode"] == "failed-job" and state["step"] == 1
        return response({"status": "completed", "completed_at": "present", "cancelled": False,
                         "devices": {IP: {"status": "failed" if failed else "success"}}})

    session.post.side_effect = post
    session.get.side_effect = get
    return session, state


def test_success_restores_original_and_uses_form_api(client):
    session, state = client
    firmware._exercise_firmware(session, IP, FILES)
    writes = [call.kwargs["data"] for call in session.post.call_args_list
              if call.args[0] == "/api/update-device"]
    assert [data["firmware_file"] for data in writes] == [TARGET, ORIGINAL]
    assert all(data["bank_mode"] == "both" for data in writes)
    assert state["version"] == BASELINE["firmware_version"]


@pytest.mark.parametrize("mode, message", [("wrong-version", "expected target"),
                                            ("failed-job", "job failed")])
def test_upgrade_failure_still_restores(client, mode, message):
    session, state = client
    state["mode"] = mode
    with pytest.raises(firmware.FirmwareCheckError, match=message):
        firmware._exercise_firmware(session, IP, FILES)
    assert state["step"] == 2
    assert state["version"] == BASELINE["firmware_version"]


@pytest.mark.parametrize("seen", [None, "", "bad timestamp", "1970-01-01T00:01:40+00:00",
                                  "1970-01-01T00:01:39+00:00", "2099-01-01T00:00:00+00:00"])
def test_stale_or_missing_observation_fails(client, seen):
    session, _ = client
    session.get.side_effect = lambda _: response({"sites": [{"aps": [{**BASELINE, "last_seen": seen}]}]})
    with pytest.raises(firmware.FirmwareCheckError, match="observation timed out"):
        firmware._fresh_ap(session, IP, 100, timeout=3)


def test_stale_baseline_prevents_writes(client):
    session, state = client
    state["mode"] = "stale"
    with pytest.raises(firmware.FirmwareCheckError, match="observation timed out"):
        firmware._exercise_firmware(session, IP, FILES)
    assert state["step"] == 0


@pytest.mark.parametrize("job", [
    {"status": "running", "completed_at": None},
    {"status": "completed", "completed_at": None},
    {"status": "pending", "completed_at": "present"},
])
def test_incomplete_job_is_rejected(client, job):
    session, _ = client
    session.get.side_effect = lambda _: response(job)
    with pytest.raises(firmware.FirmwareCheckError, match="completion timed out"):
        firmware._wait_for_job(session, "job", IP, timeout=3)


@pytest.mark.parametrize("status", ["failed", "skipped", "cancelled", "pending"])
def test_completed_job_requires_device_success(client, status):
    session, _ = client
    session.get.side_effect = lambda _: response({"status": "completed", "completed_at": "present",
                                                 "devices": {IP: {"status": status}}})
    with pytest.raises(firmware.FirmwareCheckError, match="job failed"):
        firmware._wait_for_job(session, "job", IP)


@pytest.mark.parametrize("change", ["rollback-missing", "target-missing", "target-older",
                                    "wrong-family", "unhealthy", "unknown-model", "missing-version",
                                    "missing-bank", "mixed-banks"])
def test_preflight_fails_before_any_write(client, change):
    session, state = client
    files = [dict(item) for item in FILES]
    if change == "rollback-missing":
        files.pop(0)
    elif change == "target-missing":
        files.pop()
    elif change == "target-older":
        files[1]["name"] = TARGET.replace("r200", "r050")
    elif change == "wrong-family":
        files[0]["name"] = ORIGINAL.replace("tna-30x", "tna-303l")
    elif change == "unhealthy":
        files[0]["deployable"] = False
    else:
        ap = dict(BASELINE, last_seen="1970-01-01T00:01:41+00:00")
        key, value = {"unknown-model": ("model", "unknown"), "missing-version": ("firmware_version", ""),
                      "missing-bank": ("active_bank", None), "mixed-banks": ("bank2_version", "other")}[change]
        ap[key] = value
        session.get.side_effect = lambda _: response({"sites": [{"aps": [ap]}]})
    with pytest.raises(firmware.FirmwareCheckError):
        firmware._exercise_firmware(session, IP, files)
    assert state["step"] == 0


def test_restoration_bank_mismatch_requires_operator(client):
    session, state = client
    state["mode"] = "bank-mismatch"
    with pytest.raises(firmware.FirmwareCheckError, match="operator recovery"):
        firmware._exercise_firmware(session, IP, FILES)


def test_unset_device_environment_skips_without_contact(client, monkeypatch):
    session, _ = client
    monkeypatch.delenv("SIXTYOPS_TEST_FIRMWARE_AP_IP", raising=False)
    with pytest.raises(pytest.skip.Exception, match="required"):
        firmware.test_single_device_firmware_update_and_rollback(session, FILES)
    session.get.assert_not_called()
    session.post.assert_not_called()


def test_failure_output_keeps_device_values_private(client, monkeypatch, capsys):
    session, state = client
    state["mode"] = "exception"
    monkeypatch.setenv("SIXTYOPS_TEST_FIRMWARE_AP_IP", IP)
    with pytest.raises(pytest.fail.Exception) as failure:
        firmware.test_single_device_firmware_update_and_rollback(session, FILES)
    output = str(failure.value) + str(capsys.readouterr())
    assert all(value not in output for value in (IP, "PRIVATE_DEVICE_RESPONSE", ORIGINAL, TARGET))


def test_lost_submission_response_requires_operator_without_overlapping_write(client):
    session, state = client
    state["mode"] = "exception"
    with pytest.raises(firmware.FirmwareCheckError, match="operator recovery"):
        firmware._exercise_firmware(session, IP, FILES)
    assert state["step"] == 1


def test_running_upgrade_blocks_recovery_write(client):
    session, state = client
    original_get = session.get.side_effect
    session.get.side_effect = lambda path: (response({"status": "running"})
                                            if path.startswith("/api/job/") else original_get(path))
    with pytest.raises(firmware.FirmwareCheckError, match="operator recovery"):
        firmware._exercise_firmware(session, IP, FILES)
    assert state["step"] == 1


def test_recovery_accepts_already_current_only_with_fresh_original_state(client):
    session, state = client
    state["mode"] = "failed-job"
    original_post = session.post.side_effect

    def post(path, **kwargs):
        if path == "/api/update-device" and state["step"] == 1:
            state["step"] += 1
            state["version"] = BASELINE["firmware_version"]
            return response({"status": "already_current"})
        return original_post(path, **kwargs)

    session.post.side_effect = post
    with pytest.raises(firmware.FirmwareCheckError, match="job failed"):
        firmware._exercise_firmware(session, IP, FILES)
    assert state["step"] == 2


@pytest.mark.parametrize("stale_call", [2, 3, 4])
def test_each_post_write_and_pre_recovery_read_requires_fresh_observation(client, stale_call):
    session, state = client
    original_get = session.get.side_effect
    reads = 0

    def get(path):
        nonlocal reads
        result = original_get(path)
        if path == "/api/topology":
            reads += 1
            if stale_call <= reads < stale_call + 299:
                ap = result.json()["sites"][0]["switches"][0]["aps"][0]
                ap["last_seen"] = "1970-01-01T00:01:39+00:00"
        return result

    session.get.side_effect = get
    with pytest.raises(firmware.FirmwareCheckError):
        firmware._exercise_firmware(session, IP, FILES)
    assert state["step"] == (1 if stale_call == 3 else 2)


@pytest.mark.parametrize("platform, model, field", [
    ("tna-303l", "TNA-303L-65", "firmware_file_303l"),
    ("tns-100", "TNS-100", "firmware_file_tns100"),
])
def test_model_family_selects_matching_image_and_form_field(client, platform, model, field):
    session, _ = client
    ap = dict(BASELINE, model=model)
    files = [{**item, "name": item["name"].replace("tna-30x", platform)} for item in FILES]
    target, _ = firmware._select_images(ap, files)
    firmware._start_update(session, IP, target[0])
    assert session.post.call_args.kwargs["data"][field] == target[0]


@pytest.mark.parametrize("seen, fresh", [
    ("1970-01-01T00:01:41", True),
    ("1970-01-01T00:01:41+00:00", True),
    ("1969-12-31T18:01:41-06:00", True),
    ("1970-01-01T00:01:39", False),
    ("1970-01-01T00:01:39+00:00", False),
    ("1970-01-01T00:01:40", False),
    ("1970-01-01T00:01:40+00:00", False),
    ("1970-01-01T00:01:46", False),
    ("1970-01-01T00:01:46+00:00", False),
])
def test_observation_timezone_is_independent_of_runner(client, monkeypatch, seen, fresh):
    session, _ = client
    session.get.side_effect = lambda _: response({"sites": [{"aps": [{**BASELINE, "last_seen": seen}]}]})
    try:
        with monkeypatch.context() as context:
            context.setenv("TZ", "America/Chicago")
            firmware.time.tzset()
            if fresh:
                assert firmware._fresh_ap(session, IP, 100, timeout=3)["last_seen"] == seen
            else:
                with pytest.raises(firmware.FirmwareCheckError, match="observation timed out"):
                    firmware._fresh_ap(session, IP, 100, timeout=3)
    finally:
        firmware.time.tzset()
