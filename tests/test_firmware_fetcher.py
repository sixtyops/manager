"""Tests for the Freshdesk release-page parser used by the firmware fetcher."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from updater import database as db
from updater.firmware_fetcher import (
    FirmwareFetcher,
    FirmwareRelease,
    RE_VERSION_TABLE,
    _normalize_version,
)


class TestNormalizeVersion:
    def test_plain_version_unchanged(self):
        assert _normalize_version("1.12.3") == "1.12.3"

    def test_strips_leading_dot(self):
        # Captures the real-world Freshdesk artefact that made the
        # tna-30x beta cell render as "v.1.15.0 beta-1".
        assert _normalize_version(".1.15.0") == "1.15.0"

    def test_strips_whitespace(self):
        assert _normalize_version("  1.15.0  ") == "1.15.0"

    def test_drops_suffix_token(self):
        assert _normalize_version("1.15.0 beta-1") == "1.15.0"

    def test_empty_input(self):
        assert _normalize_version("") == ""


class TestVersionTableRegex:
    """The summary-table capture must anchor on a digit even when the
    cell HTML has a stray character between 'v' and the version."""

    def test_matches_plain_version(self):
        html = (
            '<td><strong>Latest stable</strong></td>'
            '<td>v1.12.3 - some notes</td>'
        )
        assert RE_VERSION_TABLE.findall(html) == [("Latest stable", "1.12.3")]

    def test_matches_with_stray_leading_dot(self):
        # Regression: prior regex `v([\d.]+)` greedily captured the dot,
        # yielding ".1.15.0" which never matched any download link.
        html = (
            '<td><strong>Latest beta</strong></td>'
            '<td>v.1.15.0 beta-1 - notes</td>'
        )
        assert RE_VERSION_TABLE.findall(html) == [("Latest beta", "1.15.0")]

    def test_matches_with_inline_span_and_nbsp(self):
        html = (
            '<td><strong>Latest stable</strong></td>'
            '<td><span>&nbsp;v1.12.3</span></td>'
        )
        assert RE_VERSION_TABLE.findall(html) == [("Latest stable", "1.12.3")]


# Captured shape of the tna-30x Freshdesk page (2026-05-29): the beta
# version cell contains a stray leading dot which broke the parser.
TNA_30X_HTML = """
<table>
  <tr>
    <td><strong>Latest stable</strong></td>
    <td>v1.12.3 - notes</td>
  </tr>
  <tr>
    <td><strong>Latest beta</strong></td>
    <td>v.1.15.0 beta-1 - notes</td>
  </tr>
</table>
<p>
  <a href="https://tachyon-networks.com/fw/tna-30x-1.15.0-r55142-20260521-tn-110-prs-squashfs-sysupgrade.bin">
    Version 1.15.0&nbsp;beta-1
  </a>
</p>
<p>
  <a href="https://tachyon-networks.com/fw/tna-30x-1.12.3-r55002-20260219-tn-110-prs-squashfs-sysupgrade.bin">
    Version 1.12.3
  </a>
</p>
<p>
  <a href="https://tachyon-networks.com/fw/tna-30x-1.12.2-r54944-20250828-squashfs-sysupgrade.bin">
    Version 1.12.2
  </a>
</p>
"""


class TestScrapePage:
    """End-to-end check on the parsing pipeline with httpx mocked out."""

    @pytest.fixture
    def fetcher(self, tmp_path):
        return FirmwareFetcher(
            firmware_dir=tmp_path,
            broadcast_func=AsyncMock(),
        )

    def _make_mock_client(self, html: str):
        resp = MagicMock()
        resp.text = html
        resp.raise_for_status = MagicMock()
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(return_value=resp)
        return client

    def test_parses_both_stable_and_beta_despite_dot_artifact(self, fetcher):
        """Regression for the tna-30x beta being silently dropped."""
        with patch(
            "updater.firmware_fetcher.httpx.AsyncClient",
            return_value=self._make_mock_client(TNA_30X_HTML),
        ):
            releases, warnings = asyncio.run(
                fetcher._scrape_page("tna-30x", "https://example/tna-30x")
            )

        channels = {r.channel: r for r in releases}
        assert set(channels) == {"stable", "beta"}, (
            f"Expected stable+beta, got {[(r.channel, r.version) for r in releases]}"
        )
        assert channels["stable"].version == "1.12.3"
        assert channels["beta"].version == "1.15.0"
        assert "1.15.0" in channels["beta"].filename
        assert "1.12.3" in channels["stable"].filename
        # Healthy parse → no warnings
        assert warnings == []

    def test_warns_when_summary_promises_release_but_no_link_matches(self, fetcher):
        """Parse-sanity guard: summary table promised a beta, but the
        download-link parser found nothing matching that version.

        Simulates a future vendor-side HTML drift the normaliser can't
        rescue (e.g. version cell renders as "v1.15.0" but the download
        link's <a> text omits "Version" entirely, or carries a wholly
        different version number)."""
        html = """
        <table>
          <tr>
            <td><strong>Latest stable</strong></td>
            <td>v1.12.3 - notes</td>
          </tr>
          <tr>
            <td><strong>Latest beta</strong></td>
            <td>v1.15.0 beta-1 - notes</td>
          </tr>
        </table>
        <p>
          <a href="https://tachyon-networks.com/fw/tna-30x-1.12.3-r55002-20260219-tn-110-prs-squashfs-sysupgrade.bin">
            Version 1.12.3
          </a>
        </p>
        <p>
          <!-- imagine vendor changed the beta link's text shape so the
               download-link regex can no longer match -->
          <a href="https://tachyon-networks.com/fw/tna-30x-1.15.0-r55142-20260521-tn-110-prs-squashfs-sysupgrade.bin">
            Download Beta
          </a>
        </p>
        """
        with patch(
            "updater.firmware_fetcher.httpx.AsyncClient",
            return_value=self._make_mock_client(html),
        ):
            releases, warnings = asyncio.run(
                fetcher._scrape_page("tna-30x", "https://example/tna-30x")
            )

        # Stable is fine
        assert [r.channel for r in releases] == ["stable"]
        # Beta dropped silently → must surface a warning
        assert len(warnings) == 1
        w = warnings[0]
        assert "tna-30x" in w
        assert "beta" in w
        assert "1.15.0" in w

    def test_no_warning_for_summary_less_page(self, fetcher):
        """TNS-100-shape pages have no summary table; the 'first link is
        stable' fallback applies and the guard must stay silent."""
        html = """
        <p>
          <a href="https://tachyon-networks.com/fw/tns-1.12.8-r54729-20251121-tns-100-squashfs-sysupgrade.bin">
            Version 1.12.8
          </a>
        </p>
        <p>
          <a href="https://tachyon-networks.com/fw/tns-1.12.7-r54500-20251020-tns-100-squashfs-sysupgrade.bin">
            Version 1.12.7
          </a>
        </p>
        """
        with patch(
            "updater.firmware_fetcher.httpx.AsyncClient",
            return_value=self._make_mock_client(html),
        ):
            releases, warnings = asyncio.run(
                fetcher._scrape_page("tns-100", "https://example/tns-100")
            )

        assert len(releases) == 1
        assert releases[0].channel == "stable"
        assert releases[0].version == "1.12.8"
        assert warnings == []


class TestAutoSelectPin:
    """Auto-select must advance un-pinned families to the newest firmware but
    leave an operator's pinned version alone — the core of the bug where a
    manually chosen older firmware reverted to the latest beta on save."""

    STABLE_30X = "tna-30x-1.12.3-r55002-20260219-tn-110-prs-squashfs-sysupgrade.bin"
    BETA_30X = "tna-30x-1.15.0-r55142-20260521-tn-110-prs-squashfs-sysupgrade.bin"

    @pytest.fixture
    def fetcher(self, tmp_path):
        # Both firmware files present on disk; channels registered below.
        (tmp_path / self.STABLE_30X).write_bytes(b"x")
        (tmp_path / self.BETA_30X).write_bytes(b"x")
        return FirmwareFetcher(firmware_dir=tmp_path, broadcast_func=AsyncMock())

    def _register_channels(self):
        db.set_setting("firmware_channels", json.dumps({
            self.STABLE_30X: "stable",
            self.BETA_30X: "beta",
        }))

    def test_unpinned_family_advances_to_newest_beta(self, fetcher, mock_db):
        self._register_channels()
        fetcher.reselect(beta_enabled=True)
        assert db.get_setting("selected_firmware_30x", "") == self.BETA_30X

    def test_pinned_family_is_not_overwritten(self, fetcher, mock_db):
        self._register_channels()
        db.set_setting("selected_firmware_30x", self.STABLE_30X)
        db.set_setting("selected_firmware_30x_pinned", "true")
        fetcher.reselect(beta_enabled=True)
        assert db.get_setting("selected_firmware_30x", "") == self.STABLE_30X
        assert db.get_setting("selected_firmware_30x_pinned", "") == "true"

    def test_missing_pinned_file_falls_back_to_auto(self, fetcher, mock_db):
        # Uptime safety: a pin pointing at a file that's gone must not strand the
        # scheduler on a missing target — clear the pin and re-derive.
        self._register_channels()
        db.set_setting("selected_firmware_30x", "tna-30x-9.9.9-rgone.bin")
        db.set_setting("selected_firmware_30x_pinned", "true")
        fetcher.reselect(beta_enabled=True)
        assert db.get_setting("selected_firmware_30x_pinned", "") == "false"
        assert db.get_setting("selected_firmware_30x", "") == self.BETA_30X
class TestSuspectTruncatedSize:
    """Size-outlier guard (#214): a file far smaller than its same-platform
    siblings is treated as a truncated download. Catches the real case where a
    6.7 MB beta landed next to an ~18 MB stable."""

    F30X = "tna-30x-1.15.0-r55142-20260521-tn-110-prs-squashfs-sysupgrade.bin"
    SIB30X = "tna-30x-1.12.3-r55002-20260219-tn-110-prs-squashfs-sysupgrade.bin"
    SIB303L = "tna-303l-1.15.0-r8503-20260521-sysupgrade.bin"

    @pytest.fixture
    def fetcher(self, tmp_path):
        return FirmwareFetcher(firmware_dir=tmp_path, broadcast_func=AsyncMock())

    def test_outlier_small_is_flagged(self, fetcher, tmp_path):
        (tmp_path / self.SIB30X).write_bytes(b"x" * 1000)
        reason = fetcher._suspect_truncated_size(self.F30X, 400)  # < 50% of 1000
        assert reason and self.F30X in reason

    def test_comparable_size_is_ok(self, fetcher, tmp_path):
        (tmp_path / self.SIB30X).write_bytes(b"x" * 1000)
        assert fetcher._suspect_truncated_size(self.F30X, 900) is None

    def test_only_other_platform_sibling_is_ok(self, fetcher, tmp_path):
        # A 303L file is not a comparison basis for a 30x download.
        (tmp_path / self.SIB303L).write_bytes(b"x" * 1000)
        assert fetcher._suspect_truncated_size(self.F30X, 50) is None

    def test_customer_scenario(self, fetcher, tmp_path):
        with open(tmp_path / self.SIB30X, "wb") as f:
            f.truncate(18_600_000)  # sparse ~18.6 MB stable
        assert fetcher._suspect_truncated_size(self.F30X, 6_700_000) is not None


class TestDownloadIntegrity:
    """`_download_firmware` rejects truncated/corrupt transfers (#214) so a
    partial image never gets renamed into place or auto-selected."""

    @pytest.fixture
    def fetcher(self, tmp_path):
        return FirmwareFetcher(firmware_dir=tmp_path, broadcast_func=AsyncMock())

    def _release(self):
        name = "tna-30x-1.15.0-r55142-20260521-tn-110-prs-squashfs-sysupgrade.bin"
        return FirmwareRelease(
            platform="tna-30x", version="1.15.0",
            download_url=f"https://tachyon-networks.com/fw/{name}",
            channel="beta", filename=name,
        )

    def _patch_stream(self, body: bytes, content_length):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.headers = {} if content_length is None else {"content-length": str(content_length)}

        async def aiter_bytes(chunk_size=65536):
            yield body

        resp.aiter_bytes = aiter_bytes
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.stream = MagicMock(return_value=cm)
        return patch("updater.firmware_fetcher.httpx.AsyncClient", return_value=client)

    def test_good_download_accepted(self, fetcher, tmp_path):
        rel = self._release()
        with self._patch_stream(b"x" * 1000, content_length=1000):
            ok = asyncio.run(fetcher._download_firmware(rel))
        assert ok is True
        assert (tmp_path / rel.filename).stat().st_size == 1000

    def test_content_length_mismatch_rejected(self, fetcher, tmp_path):
        rel = self._release()
        with self._patch_stream(b"x" * 600, content_length=1000):  # truncated
            ok = asyncio.run(fetcher._download_firmware(rel))
        assert ok is False
        assert not (tmp_path / rel.filename).exists()
        assert list(tmp_path.glob("*.downloading")) == []

    def test_size_outlier_rejected(self, fetcher, tmp_path):
        # No Content-Length advertised, but far smaller than an existing sibling.
        sib = "tna-30x-1.12.3-r55002-20260219-tn-110-prs-squashfs-sysupgrade.bin"
        (tmp_path / sib).write_bytes(b"x" * 1000)
        rel = self._release()
        with self._patch_stream(b"x" * 100, content_length=None):
            ok = asyncio.run(fetcher._download_firmware(rel))
        assert ok is False
        assert not (tmp_path / rel.filename).exists()
