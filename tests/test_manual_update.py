"""Manual (operator-clicked) firmware update routes.

Regression coverage for the bug where clicking "update now" on a 303L AP
(beta1 -> beta2) silently did nothing: the manual routes ran the explicitly
chosen device through the fleet "needs update?" heuristic and dropped it when
the heuristic guessed no update was needed. Manual updates must push the chosen
firmware unless the device is provably already on that exact build.
"""
from unittest.mock import patch

import pytest

from updater import database as db
from updater import app as app_module

# Clean vendor filenames (no "beta" token — the channel is decided elsewhere).
FW_30X = "tna-30x-1.15.0-r55142-20260521-tn-110-prs-squashfs-sysupgrade.bin"
FW_303L_BETA2 = "tna-303l-1.5.0-r54980-20260521-sysupgrade.bin"  # -> 1.5.0.54980

AP_IP = "10.0.0.50"
MODEL_303L = "TNA-303L-65"


@pytest.fixture
def fw_dir(tmp_path):
    """Point the app at a temp firmware dir holding our test .bin files."""
    (tmp_path / FW_30X).write_bytes(b"x")
    (tmp_path / FW_303L_BETA2).write_bytes(b"x")
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
        "bank_mode": "one",
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
                    ap_ips=[AP_IP],
                    firmware_file=FW_30X,
                    firmware_file_303l=FW_303L_BETA2,
                    bank_mode="one",
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
                    ap_ips=[AP_IP],
                    firmware_file=FW_30X,
                    firmware_file_303l="",
                    bank_mode="one",
                )

        spawn.assert_not_called()


class TestUpdateDevice:
    def _update_device(self, authed_client, **extra):
        data = {
            "ip": AP_IP,
            "firmware_file": FW_30X,
            "bank_mode": "one",
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

    def test_exact_build_reports_already_current(self, authed_client, mock_db, fw_dir):
        _seed_303l_ap("1.5.0.54980")
        with patch("updater.app._spawn_update_job") as spawn:
            resp = self._update_device(authed_client)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "already_current"
        assert body["version"] == "1.5.0.54980"
        spawn.assert_not_called()

    def test_no_matching_firmware_fails_clearly(self, authed_client, mock_db, fw_dir):
        _seed_303l_ap("1.5.0.54970")
        with patch("updater.app._spawn_update_job") as spawn:
            resp = self._update_device(authed_client, firmware_file_303l="")
        assert resp.status_code == 400
        assert MODEL_303L in resp.json()["detail"]
        spawn.assert_not_called()
