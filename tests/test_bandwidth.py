"""Tests for bandwidth throttling."""

from unittest.mock import patch, AsyncMock, MagicMock

import pytest


class TestBandwidthSetting:
    """Test bandwidth_limit_kbps setting via API."""

    def test_save_bandwidth_setting(self, authed_client):
        resp = authed_client.put("/api/settings", json={
            "bandwidth_limit_kbps": "500",
        })
        assert resp.status_code == 200

    def test_save_zero_unlimited(self, authed_client):
        resp = authed_client.put("/api/settings", json={
            "bandwidth_limit_kbps": "0",
        })
        assert resp.status_code == 200

    def test_reject_negative(self, authed_client):
        resp = authed_client.put("/api/settings", json={
            "bandwidth_limit_kbps": "-1",
        })
        assert resp.status_code == 400

    def test_reject_too_large(self, authed_client):
        resp = authed_client.put("/api/settings", json={
            "bandwidth_limit_kbps": "2000000",
        })
        assert resp.status_code == 400

    def test_reject_non_numeric(self, authed_client):
        resp = authed_client.put("/api/settings", json={
            "bandwidth_limit_kbps": "fast",
        })
        assert resp.status_code == 400

    def test_viewer_cannot_change(self, viewer_client):
        resp = viewer_client.put("/api/settings", json={
            "bandwidth_limit_kbps": "500",
        })
        assert resp.status_code == 403


class TestUploadFirmwareBandwidth:
    """Test that bandwidth limit is passed to curl."""

    @pytest.mark.asyncio
    async def test_no_limit_rate_when_zero(self):
        from updater.tachyon import TachyonClient
        client = TachyonClient("10.0.0.1", "admin", "pass")
        client._token = "test"

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate.return_value = (b'{"status":"ok"}', b'')
            mock_exec.return_value = mock_proc

            await client.upload_firmware("/tmp/fw.bin", bandwidth_limit_kbps=0)

            cmd = mock_exec.call_args[0]
            assert "--limit-rate" not in cmd

    @pytest.mark.asyncio
    async def test_limit_rate_when_set(self):
        from updater.tachyon import TachyonClient
        client = TachyonClient("10.0.0.1", "admin", "pass")
        client._token = "test"

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate.return_value = (b'{"status":"ok"}', b'')
            mock_exec.return_value = mock_proc

            await client.upload_firmware("/tmp/fw.bin", bandwidth_limit_kbps=500)

            cmd = mock_exec.call_args[0]
            assert "--limit-rate" in cmd
            rate_idx = cmd.index("--limit-rate")
            assert cmd[rate_idx + 1] == "500k"

    @pytest.mark.asyncio
    async def test_update_firmware_passes_bandwidth(self):
        from updater.tachyon import TachyonClient
        client = TachyonClient("10.0.0.1", "admin", "pass")

        with patch.object(client, "login", new_callable=AsyncMock, return_value=True), \
             patch.object(client, "get_device_info", new_callable=AsyncMock) as mock_info, \
             patch.object(client, "upload_firmware", new_callable=AsyncMock, return_value=True) as mock_upload, \
             patch.object(client, "trigger_update", new_callable=AsyncMock, return_value=True), \
             patch.object(client, "wait_for_reboot", new_callable=AsyncMock, return_value=True), \
             patch.object(client, "get_device_info", new_callable=AsyncMock) as mock_info2:

            # Setup mock info
            info = MagicMock()
            info.current_version = "1.0"
            info.model = "T5c"
            info.bank1_version = "1.0"
            info.bank2_version = "0.9"
            info.active_bank = 1
            mock_info.return_value = info

            info2 = MagicMock()
            info2.current_version = "2.0"
            info2.model = "T5c"
            info2.bank1_version = "2.0"
            info2.bank2_version = "1.0"
            info2.active_bank = 1
            mock_info2.return_value = info2

            # Override get_device_info to return different values
            call_count = [0]
            async def get_info_side_effect():
                call_count[0] += 1
                return info if call_count[0] == 1 else info2
            client.get_device_info = get_info_side_effect

            result = await client.update_firmware(
                "/tmp/fw.bin",
                bandwidth_limit_kbps=1000,
            )

            mock_upload.assert_called_once_with("/tmp/fw.bin", bandwidth_limit_kbps=1000)
