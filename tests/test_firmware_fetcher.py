"""Tests for the Freshdesk release-page parser used by the firmware fetcher."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from updater.firmware_fetcher import (
    FirmwareFetcher,
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
