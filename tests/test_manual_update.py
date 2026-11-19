"""Manual (operator-clicked) firmware update routes.

Regression coverage for the bug where clicking "update now" on a 303L AP
(beta1 -> beta2) silently did nothing: the manual routes ran the explicitly
chosen device through the fleet "needs update?" heuristic and dropped it when
the heuristic guessed no update was needed. Manual updates must push the chosen
firmware unless the device is provably already on that exact build.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from updater import database as db
from updater import app as app_module
from updater.vendors.tachyon.client import BootBankRead

# Clean vendor filenames (no "beta" token — the channel is decided elsewhere).
FW_30X = "tna-30x-1.15.0-r55142-20260521-tn-110-prs-squashfs-sysupgrade.bin"
FW_303L_BETA2 = "tna-303l-1.5.0-r54980-20260521-sysupgrade.bin"  # -> 1.5.0.54980
FW_303L_CPE_TARGET = "tna-303l-1.15.1-r55177-20260521-sysupgrade.bin"

AP_IP = "10.0.0.50"
MODEL_303L = "TNA-303L-65"


@pytest.fixture(autouse=True)
def live_banks():
    with patch("updater.vendors.tachyon.TachyonDriver.connect", new_callable=AsyncMock, return_value=True), \
         patch("updater.vendors.tachyon.TachyonDriver.get_bootbank_info", new_callable=AsyncMock) as read:
        read.return_value = BootBankRead("1.5.0.54980", "1.5.0.54970", 1)
        yield read


@pytest.fixture
def fw_dir(tmp_path):
    """Point the app at a temp firmware dir holding our test .bin files."""
    (tmp_path / FW_30X).write_bytes(b"x")
    (tmp_path / FW_303L_BETA2).write_bytes(b"x")
    (tmp_path / FW_303L_CPE_TARGET).write_bytes(b"x")
    with patch("updater.app.FIRMWARE_DIR", tmp_path):
        yield tmp_path


def _seed_303l_ap(firmware_version: str):
    db.upsert_access_point(
        AP_IP, "root", "pass",
        model=MODEL_303L, firmware_version=firmware_version,
    )


def _start_update(authed_client, **extra):
    data = {
        "firmware_file": FW_30X,
        "device_type": "mixed",
        "ip_list": AP_IP,
        "firmware_file_303l": FW_303L_BETA2,
    }
    data.update(extra)
    return authed_client.post("/api/start-update", data=data)


class TestStartUpdate:
    def test_enrolls_ap_behind_target(self, authed_client, mock_db, fw_dir):
        """beta1 -> beta2: the AP is enrolled and a job starts."""
        _seed_303l_ap("1.5.0.54970")
        with patch("updater.app._spawn_update_job") as spawn:
            resp = _start_update(authed_client)
        assert resp.status_code == 200, resp.text
        assert resp.json().get("job_id")
        spawn.assert_called_once()
        assert spawn.call_args.args[0].bank_mode == "one"

    @pytest.mark.asyncio
    async def test_scheduled_job_defaults_to_one_bank(self, mock_db, tmp_path):
        """Omitting bank_mode from scheduled job creation selects one bank."""
        (tmp_path / FW_30X).write_bytes(b"x")
        (tmp_path / FW_303L_BETA2).write_bytes(b"x")
        _seed_303l_ap("1.5.0.54970")

        with patch("updater.app.FIRMWARE_DIR", tmp_path), \
             patch("updater.app._scheduled_action_decision", return_value=(True, None)), \
             patch("updater.app._spawn_update_job") as spawn:
            job_id = await app_module._start_scheduled_update(
                ap_ips=[],
                firmware_file=FW_30X,
                firmware_file_303l=FW_303L_BETA2,
                device_entries=[{"ip": AP_IP, "role": "ap"}],
            )

        assert job_id
        spawn.assert_called_once()
        assert spawn.call_args.args[0].bank_mode == "one"

    def test_enrolls_ap_that_parses_as_ahead(self, authed_client, mock_db, fw_dir):
        """The core fix: a device whose build parses as *newer* than the chosen
        target (allow_downgrade off) must still be pushed on an explicit click,
        not silently skipped as 'already current'."""
        _seed_303l_ap("1.5.0.54990")  # higher build number than target 54980
        with patch("updater.app._spawn_update_job") as spawn:
            resp = _start_update(authed_client)
        assert resp.status_code == 200, resp.text
        assert resp.json().get("job_id")
        spawn.assert_called_once()

    def test_ahead_cpe_still_enrolled_on_manual_update(self, authed_client, mock_db, fw_dir):
        """A manual AP/site update must push the selected firmware to the attached
        CPEs the operator included — even ones whose build parses as *newer* than
        the target (allow_downgrade off). The fleet 'needs update?' heuristic would
        silently skip them; manual enrollment uses the exact-build gate like the AP."""
        # AP already on the exact target, so only the CPE can drive the job.
        _seed_303l_ap("1.5.0.54980")
        db.upsert_cpe(AP_IP, {
            "ip": "10.0.0.60",
            "model": MODEL_303L,
            "firmware_version": "1.5.0.55000",  # parses ahead of target 1.5.0.54980
            "auth_status": "ok",
        })
        with patch("updater.app._spawn_update_job") as spawn:
            resp = _start_update(authed_client, bank_mode="one")
        assert resp.status_code == 200, resp.text
        assert resp.json().get("job_id")
        spawn.assert_called_once()

    def test_exact_build_cpe_is_skipped_on_manual_update(self, authed_client, mock_db, fw_dir):
        """The flip side: a CPE already on the exact target build is not re-flashed,
        so an all-current AP+CPE set is a neutral no-op, not a needless reboot."""
        _seed_303l_ap("1.5.0.54980")
        db.upsert_cpe(AP_IP, {
            "ip": "10.0.0.60",
            "model": MODEL_303L,
            "firmware_version": "1.5.0.54980",  # exact target
            "auth_status": "ok",
        })
        with patch("updater.app._spawn_update_job") as spawn:
            resp = _start_update(authed_client, bank_mode="one")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "already_current"}
        spawn.assert_not_called()

    def test_exact_build_is_neutral_noop(self, authed_client, mock_db, fw_dir):
        """Already on the exact build: no job, no reboot, no scary error."""
        _seed_303l_ap("1.5.0.54980")
        with patch("updater.app._spawn_update_job") as spawn:
            resp = _start_update(authed_client)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "already_current"}
        spawn.assert_not_called()

    def test_no_matching_firmware_fails_clearly(self, authed_client, mock_db, fw_dir):
        """303L AP with no 303L firmware selected -> clear 400 naming the missing
        family and the affected device, not a silent fall back to the 30x file."""
        _seed_303l_ap("1.5.0.54970")
        with patch("updater.app._spawn_update_job") as spawn:
            resp = _start_update(authed_client, firmware_file_303l="")
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "TNA-303L" in detail   # names the missing firmware family
        assert AP_IP in detail        # and the affected device
        spawn.assert_not_called()

    def test_missing_family_aborts_whole_batch(self, authed_client, mock_db, fw_dir):
        """One device whose family firmware is missing refuses the *entire* batch
        (compatibility can't be guaranteed) in one message listing every affected
        device — nothing is flashed, not even the families that were available."""
        ap_ip2 = "10.0.0.51"
        _seed_303l_ap("1.5.0.54970")
        db.upsert_access_point(
            ap_ip2, "root", "pass", model=MODEL_303L, firmware_version="1.5.0.54970",
        )
        with patch("updater.app._spawn_update_job") as spawn:
            resp = _start_update(
                authed_client, ip_list=f"{AP_IP}\n{ap_ip2}", firmware_file_303l="",
            )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "TNA-303L" in detail
        assert "2 devices" in detail
        assert AP_IP in detail and ap_ip2 in detail
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_scheduled_update_missing_family_file_fails_before_job(self, mock_db, tmp_path):
        """Scheduler path must fail closed instead of silently dropping the
        303L target or falling back to the 30x image."""
        (tmp_path / FW_30X).write_bytes(b"x")
        _seed_303l_ap("1.5.0.54970")

        with patch("updater.app.FIRMWARE_DIR", tmp_path), \
             patch("updater.app._spawn_update_job") as spawn:
            with pytest.raises(RuntimeError, match="303L firmware file not found"):
                await app_module._start_scheduled_update(
                    ap_ips=[],
                    firmware_file=FW_30X,
                    firmware_file_303l=FW_303L_BETA2,
                    bank_mode="one",
                    device_entries=[{"ip": AP_IP, "role": "ap"}],
                )

        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_scheduled_update_no_family_selected_names_family(self, mock_db, tmp_path):
        """When a 303L device is in scope but no 303L file is selected at all, the
        scheduler refuses with the same family-named reason the operator sees via
        the scheduler block reason — not a bare/first-device error."""
        (tmp_path / FW_30X).write_bytes(b"x")
        _seed_303l_ap("1.5.0.54970")

        with patch("updater.app.FIRMWARE_DIR", tmp_path), \
             patch("updater.app._spawn_update_job") as spawn:
            with pytest.raises(RuntimeError, match="TNA-303L"):
                await app_module._start_scheduled_update(
                    ap_ips=[],
                    firmware_file=FW_30X,
                    firmware_file_303l="",
                    bank_mode="one",
                    device_entries=[{"ip": AP_IP, "role": "ap"}],
                )

        spawn.assert_not_called()


class TestExactBuildGate:
    """Document the manual exact-build gate, including its dual-bank semantics."""

    TARGET = "1.5.0.54980"

    def test_one_bank_active_match_is_on_exact_build(self):
        device = {"firmware_version": self.TARGET}
        assert app_module._device_on_exact_build(device, self.TARGET, "one") is True

    def test_one_bank_active_mismatch_is_not_on_exact_build(self):
        device = {"firmware_version": "1.5.0.55000"}  # parses newer, still not exact
        assert app_module._device_on_exact_build(device, self.TARGET, "one") is False

    def test_both_banks_on_target_is_on_exact_build(self):
        device = {
            "firmware_version": self.TARGET,
            "active_bank": 1,
            "bank1_version": self.TARGET,
            "bank2_version": self.TARGET,
        }
        assert app_module._device_on_exact_build(device, self.TARGET, "both") is True

    def test_both_mode_unknown_inactive_bank_is_reflashed(self):
        """Dual-bank: with no inactive-bank data we can't prove the write is
        complete, so an exact active version is NOT a no-op — it reflashes."""
        device = {"firmware_version": self.TARGET}  # active matches, inactive unknown
        assert app_module._device_on_exact_build(device, self.TARGET, "both") is False
        # Same device in single-bank mode IS on the exact build.
        assert app_module._device_on_exact_build(device, self.TARGET, "one") is True


class TestUpdateDevice:
    def _update_device(self, authed_client, **extra):
        data = {
            "ip": AP_IP,
            "firmware_file": FW_30X,
            "firmware_file_303l": FW_303L_BETA2,
        }
        data.update(extra)
        return authed_client.post("/api/update-device", data=data)

    def test_enrolls_ap_behind_target(self, authed_client, mock_db, fw_dir):
        _seed_303l_ap("1.5.0.54970")
        with patch("updater.app._spawn_update_job") as spawn:
            resp = self._update_device(authed_client)
        assert resp.status_code == 200, resp.text
        assert resp.json().get("job_id")
        spawn.assert_called_once()
        assert spawn.call_args.args[0].bank_mode == "one"

    def test_exact_build_reports_already_current(self, authed_client, mock_db, fw_dir):
        _seed_303l_ap("1.5.0.54980")
        with patch("updater.app._spawn_update_job") as spawn:
            resp = self._update_device(authed_client)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "already_current"
        assert body["version"] == "1.5.0.54980"
        spawn.assert_not_called()

    def test_cpe_uses_unified_active_bank_for_exact_build(self, authed_client, mock_db, fw_dir):
        cpe_ip = "10.0.0.60"
        _seed_303l_ap("1.5.0.54980")
        db.upsert_cpe(AP_IP, {
            "ip": cpe_ip, "model": MODEL_303L,
            "firmware_version": "1.15.1.55177", "auth_status": "ok",
        })
        db.upsert_device(
            cpe_ip, "cpe", "root", "pass", model=MODEL_303L,
            firmware_version="1.15.1.55177", active_bank=1,
            bank1_version="1.15.0.55151", bank2_version="1.15.1.55177",
        )
        with patch("updater.app._spawn_update_job") as spawn:
            resp = self._update_device(authed_client, ip=cpe_ip,
                                       firmware_file=FW_303L_CPE_TARGET, bank_mode="one")
        assert resp.status_code == 200, resp.text
        assert resp.json().get("job_id")
        spawn.assert_called_once()

    def test_cpe_active_bank_exact_build_reports_already_current(
        self, authed_client, mock_db, fw_dir, live_banks,
    ):
        live_banks.return_value = BootBankRead("1.15.0.55151", "1.15.1.55177", 2)
        cpe_ip = "10.0.0.60"
        _seed_303l_ap("1.5.0.54980")
        db.upsert_cpe(AP_IP, {
            "ip": cpe_ip, "model": MODEL_303L,
            "firmware_version": "1.15.0.55151", "auth_status": "ok",
        })
        db.upsert_device(
            cpe_ip, "cpe", "root", "pass", model=MODEL_303L,
            firmware_version="1.15.0.55151",
        )
        db.update_device_status(
            cpe_ip, active_bank=2,
            bank1_version="1.15.0.55151", bank2_version="1.15.1.55177",
        )

        with patch("updater.app._spawn_update_job") as spawn:
            resp = self._update_device(
                authed_client, ip=cpe_ip,
                firmware_file_303l=FW_303L_CPE_TARGET, bank_mode="one",
            )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "already_current", "version": "1.15.1.55177"}
        spawn.assert_not_called()

    def test_cpe_cache_only_firmware_does_not_prove_exact_build(self, authed_client, mock_db, fw_dir):
        cpe_ip = "10.0.0.60"
        _seed_303l_ap("1.5.0.54980")
        db.upsert_cpe(AP_IP, {
            "ip": cpe_ip, "model": MODEL_303L,
            "firmware_version": "1.15.1.55177", "auth_status": "ok",
        })
        with patch("updater.app._spawn_update_job") as spawn:
            resp = self._update_device(authed_client, ip=cpe_ip,
                                       firmware_file=FW_303L_CPE_TARGET, bank_mode="one")
        assert resp.status_code == 200, resp.text
        assert resp.json().get("job_id")
        spawn.assert_called_once()

    def test_exact_build_reports_already_current_during_window_stop(
        self, authed_client, mock_db, fw_dir,
    ):
        _seed_303l_ap("1.5.0.54980")
        window_key = app_module._manual_firmware_window_key()
        db.stop_firmware_window(window_key, "stopped-job", AP_IP, "flash", "Upload failed")

        with patch("updater.app._spawn_update_job") as spawn:
            resp = self._update_device(authed_client)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "already_current", "version": "1.5.0.54980"}
        spawn.assert_not_called()

    def test_real_write_stays_blocked_during_window_stop(
        self, authed_client, mock_db, fw_dir,
    ):
        _seed_303l_ap("1.5.0.54970")
        window_key = app_module._manual_firmware_window_key()
        db.stop_firmware_window(window_key, "stopped-job", AP_IP, "flash", "Upload failed")

        with patch("updater.app._spawn_update_job") as spawn:
            resp = self._update_device(authed_client)

        assert resp.status_code == 409
        spawn.assert_not_called()

    def test_no_matching_firmware_fails_clearly(self, authed_client, mock_db, fw_dir):
        _seed_303l_ap("1.5.0.54970")
        with patch("updater.app._spawn_update_job") as spawn:
            resp = self._update_device(authed_client, firmware_file_303l="")
        assert resp.status_code == 400
        assert MODEL_303L in resp.json()["detail"]
        spawn.assert_not_called()


@pytest.mark.parametrize("route", ["/api/start-update", "/api/update-device"])
def test_manual_update_available_during_safety_and_firmware_holds(
    authed_client, mock_db, fw_dir, route
):
    _seed_303l_ap("1.5.0.54970")
    db.set_settings({
        "schedule_enabled": "true",
        "scheduled_rollout_safety_ready": "false",
        "firmware_canary_hold_days": "9999",
    })
    data = {
        "ip": AP_IP, "ip_list": AP_IP, "device_type": "mixed",
        "firmware_file": FW_30X, "firmware_file_303l": FW_303L_BETA2,
        "bank_mode": "one",
    }
    with patch("updater.app._spawn_update_job") as spawn:
        response = authed_client.post(route, data=data)
    assert response.status_code == 200, response.text
    assert response.json()["job_id"]
    spawn.assert_called_once()


class TestLiveExactBuild:
    TARGET = "1.5.0.54980"
    OLD = "1.5.0.54970"
    SWITCH_FW = "tns-100-1.5.0-r54980-sysupgrade.bin"

    def _request(self, client, fw_dir, role="ap", bank_mode="one"):
        _seed_303l_ap(self.TARGET)
        ip = AP_IP
        extra = {}
        if role == "cpe":
            ip = "10.0.0.60"
            db.upsert_cpe(AP_IP, {
                "ip": ip, "model": MODEL_303L,
                "firmware_version": self.TARGET, "auth_status": "ok",
            })
            db.upsert_device(ip, "cpe", "unused", "unused", model=MODEL_303L)
        elif role == "switch":
            ip = "10.0.0.70"
            db.upsert_device(ip, "switch", "root", "pass", model="TNS-100")
            (fw_dir / self.SWITCH_FW).write_bytes(b"x")
            extra["firmware_file_tns100"] = self.SWITCH_FW
        db.update_device_status(ip, active_bank=2,
                                bank1_version=self.TARGET, bank2_version=self.TARGET)
        return TestUpdateDevice()._update_device(client, ip=ip, bank_mode=bank_mode, **extra)

    @pytest.mark.parametrize("role", ["ap", "cpe", "switch"])
    def test_stale_stored_active_bank_starts_job(
        self, authed_client, mock_db, fw_dir, live_banks, role,
    ):
        live_banks.return_value = BootBankRead(self.OLD, self.TARGET, 1)
        with patch("updater.app._spawn_update_job") as spawn:
            response = self._request(authed_client, fw_dir, role)
        assert response.status_code == 200, response.text
        assert response.json()["job_id"]
        spawn.assert_called_once()
        live_banks.assert_awaited_once()

    @pytest.mark.parametrize("role", ["ap", "cpe", "switch"])
    @pytest.mark.parametrize("mode,inactive,noop", [
        ("one", OLD, True), ("both", TARGET, True),
        ("both", OLD, False), ("both", None, False),
    ])
    def test_live_match_and_both_bank_rules(
        self, authed_client, mock_db, fw_dir, live_banks, role, mode, inactive, noop,
    ):
        live_banks.return_value = BootBankRead(inactive, "1.5.0.r54980", 2)
        with patch("updater.app._spawn_update_job") as spawn:
            response = self._request(authed_client, fw_dir, role, mode)
        assert response.status_code == 200, response.text
        if noop:
            assert response.json() == {"status": "already_current", "version": self.TARGET}
            spawn.assert_not_called()
        else:
            assert response.json()["job_id"]
            spawn.assert_called_once()

    @pytest.mark.parametrize("banks", [
        BootBankRead(TARGET, TARGET, 1, "warning"),
        BootBankRead(TARGET, TARGET, 0), BootBankRead(TARGET, TARGET, True),
        BootBankRead(TARGET, TARGET, "1"), BootBankRead(TARGET, TARGET, None),
        BootBankRead(None, TARGET, 1), BootBankRead(TARGET, None, 2),
        BootBankRead("", TARGET, 1), BootBankRead(123, TARGET, 1),
        None, {}, object(),
    ])
    def test_incomplete_or_malformed_read_cannot_use_stored_banks(
        self, authed_client, mock_db, fw_dir, live_banks, banks,
    ):
        live_banks.return_value = banks
        with patch("updater.app._spawn_update_job") as spawn:
            response = self._request(authed_client, fw_dir)
        assert response.status_code == 200, response.text
        assert response.json()["job_id"]
        spawn.assert_called_once()

    @pytest.mark.parametrize("failure", ["login", "read", "connect-timeout", "read-timeout"])
    @pytest.mark.parametrize("stopped", [False, True])
    def test_failed_proof_uses_existing_admission(
        self, authed_client, mock_db, fw_dir, live_banks, failure, stopped,
    ):
        if stopped:
            db.stop_firmware_window(app_module._manual_firmware_window_key(),
                                    "stopped-job", AP_IP, "flash", "Upload failed")
        connect = AsyncMock(return_value=True)
        if failure == "login":
            connect.return_value = False
        elif failure == "connect-timeout":
            connect.side_effect = TimeoutError
        else:
            live_banks.side_effect = TimeoutError if failure == "read-timeout" else RuntimeError
        with patch("updater.vendors.tachyon.TachyonDriver.connect", connect), \
             patch("updater.app._spawn_update_job") as spawn:
            response = self._request(authed_client, fw_dir)
        if stopped:
            assert response.status_code == 409
            spawn.assert_not_called()
        else:
            assert response.status_code == 200, response.text
            assert response.json()["job_id"]
            spawn.assert_called_once()
        if failure in ("login", "connect-timeout"):
            live_banks.assert_not_awaited()

    def test_connect_and_read_share_one_deadline(
        self, authed_client, mock_db, fw_dir, live_banks,
    ):
        real_timeout = asyncio.timeout

        async def connect():
            await asyncio.sleep(0.01)
            return True

        async def read():
            await asyncio.Event().wait()

        live_banks.side_effect = read
        with patch("updater.vendors.tachyon.TachyonDriver.connect", side_effect=connect), \
             patch("updater.app.asyncio.timeout", side_effect=lambda seconds: real_timeout(0.03)) as budget, \
             patch("updater.app._spawn_update_job") as spawn:
            response = self._request(authed_client, fw_dir)
        budget.assert_called_once_with(10)
        live_banks.assert_awaited_once()
        assert response.json()["job_id"]
        spawn.assert_called_once()

    @pytest.mark.parametrize("role", ["ap", "cpe", "switch"])
    def test_each_request_uses_fresh_driver_and_resolved_credentials(
        self, authed_client, mock_db, fw_dir, live_banks, role,
    ):
        from updater.vendors.tachyon import TachyonDriver

        original_init = TachyonDriver.__init__
        with patch.object(TachyonDriver, "__init__", autospec=True,
                          side_effect=original_init) as create:
            for _ in range(2):
                response = self._request(authed_client, fw_dir, role)
                assert response.json()["status"] == "already_current"
        assert create.call_count == 2
        first, second = create.call_args_list
        assert first.args[0] is not second.args[0]
        expected_ip = {"ap": AP_IP, "cpe": "10.0.0.60", "switch": "10.0.0.70"}[role]
        assert first.args[1:] == second.args[1:] == (expected_ip, "root", "pass")
        assert live_banks.await_count == 2
