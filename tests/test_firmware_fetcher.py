"""Tests for the Freshdesk release-page parser used by the firmware fetcher."""

import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from updater import database as db
from updater.firmware_fetcher import (
    FirmwareFetcher,
    FirmwareRelease,
    RE_TABLE_MD5,
    RE_VERSION_TABLE,
    _normalize_version,
    _parse_table_md5s,
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


class TestTableMd5:
    """Vendor-MD5 parsing reads the summary-table cells only — never the
    inline 'MD5: <hash>' notes in the release-notes body — and never binds a
    hash to another row's version."""

    def test_extracts_pairs_from_summary_table(self):
        md5s = _parse_table_md5s(TNA_30X_HTML)
        assert md5s["1.12.3"] == "883fa6b688eb017b3ac56f1300f0cad0"
        assert md5s["1.15.0"] == "ea3e1c6f657ec8dc5624feabd1ce40ff"

    def test_ignores_inline_body_md5_notes(self):
        # The body carries a beta-1 MD5 (fa3f...) only as an <em>MD5: ...</em>
        # note outside any <table>; table-scoping must drop it.
        md5s = _parse_table_md5s(TNA_30X_HTML)
        assert "fa3f7eb2d0e622e3ae4e9a829051ef33" not in md5s.values()

    def test_lowercases_hash(self):
        html = "<table><tr><td>v2.0.0</td><td>ABCDEF0123456789ABCDEF0123456789</td></tr></table>"
        assert _parse_table_md5s(html) == {"2.0.0": "abcdef0123456789abcdef0123456789"}

    def test_does_not_cross_row_boundary(self):
        # Version in row 1, 32-hex in row 2 → must NOT pair (tempered </tr>).
        html = (
            "<table>"
            "<tr><td>v1.0.0</td></tr>"
            "<tr><td>deadbeefdeadbeefdeadbeefdeadbeef</td></tr>"
            "</table>"
        )
        assert _parse_table_md5s(html) == {}

    def test_no_table_returns_empty(self):
        assert _parse_table_md5s("<p>no tables here, just v1.2.3 prose</p>") == {}

    def test_first_table_match_wins(self):
        html = (
            "<table><tr><td>v1.0.0</td><td>" + "a" * 32 + "</td></tr></table>"
            "<table><tr><td>v1.0.0</td><td>" + "b" * 32 + "</td></tr></table>"
        )
        assert _parse_table_md5s(html)["1.0.0"] == "a" * 32


# Captured shape of the tna-30x Freshdesk page (2026-06-10): the summary table
# gained an MD5 column (Type | File | Release date | MD5), with the version in
# the File cell's <a> text. The beta File cell still renders the version with a
# stray leading dot ("v.1.15.0"). The page ALSO repeats each MD5 as an inline
# "MD5: <hash>" note in the release-notes body — _parse_table_md5s must read the
# table cells, not those notes (the beta-1 hash below only appears as a note).
TNA_30X_HTML = """
<table style="width: 93%;"><tbody>
  <tr>
    <td><strong>Type</strong></td><td><strong>File</strong></td>
    <td><strong>Release date</strong></td><td><strong>MD5</strong></td>
  </tr>
  <tr>
    <td><strong>Latest stable</strong></td>
    <td>&nbsp;<a href="https://tachyon-networks.com/fw/tna-30x/1.12.3/tna-30x-1.12.3-r55002-20260219-tn-110-prs-squashfs-sysupgrade.bin">v1.12.3</a></td>
    <td>Feb 23, 2026</td>
    <td>883fa6b688eb017b3ac56f1300f0cad0</td>
  </tr>
  <tr>
    <td><strong>Latest beta</strong></td>
    <td>&nbsp;<a href="https://tachyon-networks.com/fw/tna-30x/1.15.0/tna-30x-1.15.0-r55151-20260609-tn-110-prs-squashfs-sysupgrade.bin" rel="noopener noreferrer" target="_blank">v.1.15.0</a> <strong>(beta-2)</strong></td>
    <td>June 9, 2026</td>
    <td>ea3e1c6f657ec8dc5624feabd1ce40ff</td>
  </tr>
</tbody></table>
<p>
  <a href="https://tachyon-networks.com/fw/tna-30x/1.15.0/tna-30x-1.15.0-r55151-20260609-tn-110-prs-squashfs-sysupgrade.bin">
    Version 1.15.0&nbsp;beta-2
  </a>
</p>
<p dir="ltr"><br><em>MD5: ea3e1c6f657ec8dc5624feabd1ce40ff (beta-2)</em></p>
<p>
  <a href="https://tachyon-networks.com/fw/tna-30x/1.12.3/tna-30x-1.12.3-r55002-20260219-tn-110-prs-squashfs-sysupgrade.bin">
    Version 1.12.3
  </a>
</p>
<p>
  <a href="https://tachyon-networks.com/fw/tna-30x/1.12.2/tna-30x-1.12.2-r54944-20250828-squashfs-sysupgrade.bin">
    Version 1.12.2
  </a>
</p>
<p dir="ltr"><br><em>MD5: fa3f7eb2d0e622e3ae4e9a829051ef33 (beta-1)</em></p>
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
        # Vendor MD5s attached from the summary-table cells (not the body notes)
        assert channels["stable"].md5 == "883fa6b688eb017b3ac56f1300f0cad0"
        assert channels["beta"].md5 == "ea3e1c6f657ec8dc5624feabd1ce40ff"
        # Healthy parse → no warnings
        assert warnings == []

    def test_scrape_without_md5_column_yields_none(self, fetcher):
        """A summary table predating the MD5 column → md5 is None on every
        release and fetching still works (size guards carry the integrity)."""
        html = """
        <table>
          <tr><td><strong>Latest stable</strong></td><td>v1.12.3 - notes</td></tr>
          <tr><td><strong>Latest beta</strong></td><td>v1.15.0 beta-1 - notes</td></tr>
        </table>
        <p><a href="https://tachyon-networks.com/fw/tna-30x-1.12.3-r55002-20260219-tn-110-prs-squashfs-sysupgrade.bin">Version 1.12.3</a></p>
        <p><a href="https://tachyon-networks.com/fw/tna-30x-1.15.0-r55142-20260521-tn-110-prs-squashfs-sysupgrade.bin">Version 1.15.0</a></p>
        """
        with patch(
            "updater.firmware_fetcher.httpx.AsyncClient",
            return_value=self._make_mock_client(html),
        ):
            releases, warnings = asyncio.run(
                fetcher._scrape_page("tna-30x", "https://example/tna-30x")
            )
        assert {r.channel for r in releases} == {"stable", "beta"}
        assert all(r.md5 is None for r in releases)
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
        assert releases[0].md5 is None  # no summary table → no MD5 to parse
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

    def _release(self, md5=None):
        name = "tna-30x-1.15.0-r55142-20260521-tn-110-prs-squashfs-sysupgrade.bin"
        return FirmwareRelease(
            platform="tna-30x", version="1.15.0",
            download_url=f"https://tachyon-networks.com/fw/{name}",
            channel="beta", filename=name, md5=md5,
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
        body = b"x" * 1000
        rel = self._release()
        with self._patch_stream(body, content_length=1000):
            ok, sha = asyncio.run(fetcher._download_firmware(rel))
        assert ok is True
        assert (tmp_path / rel.filename).stat().st_size == 1000
        # SHA256 is computed in the same pass and handed back for storage.
        assert sha == hashlib.sha256(body).hexdigest()

    def test_content_length_mismatch_rejected(self, fetcher, tmp_path):
        rel = self._release()
        with self._patch_stream(b"x" * 600, content_length=1000):  # truncated
            ok, sha = asyncio.run(fetcher._download_firmware(rel))
        assert ok is False
        assert sha is None
        assert not (tmp_path / rel.filename).exists()
        assert list(tmp_path.glob("*.downloading")) == []

    def test_size_outlier_rejected(self, fetcher, tmp_path):
        # No Content-Length advertised, but far smaller than an existing sibling.
        sib = "tna-30x-1.12.3-r55002-20260219-tn-110-prs-squashfs-sysupgrade.bin"
        (tmp_path / sib).write_bytes(b"x" * 1000)
        rel = self._release()
        with self._patch_stream(b"x" * 100, content_length=None):
            ok, sha = asyncio.run(fetcher._download_firmware(rel))
        assert ok is False
        assert sha is None
        assert not (tmp_path / rel.filename).exists()

    def test_md5_match_accepted(self, fetcher, tmp_path):
        body = b"firmware-payload" * 64
        rel = self._release(md5=hashlib.md5(body).hexdigest())
        with self._patch_stream(body, content_length=len(body)):
            ok, sha = asyncio.run(fetcher._download_firmware(rel))
        assert ok is True
        assert (tmp_path / rel.filename).exists()
        assert sha == hashlib.sha256(body).hexdigest()

    def test_md5_match_is_case_insensitive(self, fetcher, tmp_path):
        body = b"firmware-payload" * 64
        rel = self._release(md5=hashlib.md5(body).hexdigest().upper())
        with self._patch_stream(body, content_length=len(body)):
            ok, _ = asyncio.run(fetcher._download_firmware(rel))
        assert ok is True

    def test_md5_mismatch_rejected(self, fetcher, tmp_path):
        # Valid-shaped but wrong hash → corrupt/wrong image, never accepted.
        rel = self._release(md5="0" * 32)
        with self._patch_stream(b"x" * 1000, content_length=1000):
            ok, sha = asyncio.run(fetcher._download_firmware(rel))
        assert ok is False
        assert sha is None
        assert not (tmp_path / rel.filename).exists()
        assert list(tmp_path.glob("*.downloading")) == []

    def test_md5_absent_good_download_accepted(self, fetcher, tmp_path):
        body = b"x" * 1000
        rel = self._release(md5=None)
        with self._patch_stream(body, content_length=1000):
            ok, sha = asyncio.run(fetcher._download_firmware(rel))
        assert ok is True
        assert sha == hashlib.sha256(body).hexdigest()

    def test_md5_absent_does_not_bypass_size_guard(self, fetcher, tmp_path):
        # A missing published MD5 must not weaken the existing truncation guard.
        sib = "tna-30x-1.12.3-r55002-20260219-tn-110-prs-squashfs-sysupgrade.bin"
        (tmp_path / sib).write_bytes(b"x" * 1000)
        rel = self._release(md5=None)
        with self._patch_stream(b"x" * 100, content_length=None):
            ok, sha = asyncio.run(fetcher._download_firmware(rel))
        assert ok is False
        assert sha is None


class TestCheckAndDownloadHashing:
    """check_and_download stores a SHA256 for auto-fetched firmware so the
    pre-flash integrity check (skipped when no hash is stored) also covers
    auto files — both on fresh download and by backfilling existing files."""

    BETA = "tna-30x-1.15.0-r55151-20260609-tn-110-prs-squashfs-sysupgrade.bin"

    @pytest.fixture
    def fetcher(self, tmp_path):
        return FirmwareFetcher(firmware_dir=tmp_path, broadcast_func=AsyncMock())

    def _release(self):
        return FirmwareRelease(
            platform="tna-30x", version="1.15.0",
            download_url=f"https://tachyon-networks.com/fw/{self.BETA}",
            channel="beta", filename=self.BETA,
            md5="ea3e1c6f657ec8dc5624feabd1ce40ff",
        )

    def _scrape_only_30x(self, release):
        """AsyncMock side-effect: yield `release` for tna-30x, nothing else."""
        async def fake_scrape(platform, url):
            return ([release], []) if platform == "tna-30x" else ([], [])
        return fake_scrape

    def test_fresh_download_registers_sha256(self, fetcher, tmp_path, mock_db):
        rel = self._release()
        sha = "d" * 64

        async def fake_dl(release):
            (tmp_path / release.filename).write_bytes(b"fw")  # simulate landing
            return True, sha

        with patch.object(fetcher, "_scrape_page",
                          AsyncMock(side_effect=self._scrape_only_30x(rel))), \
             patch.object(fetcher, "_download_firmware", AsyncMock(side_effect=fake_dl)):
            asyncio.run(fetcher.check_and_download())

        assert db.get_firmware_sha256(self.BETA) == sha

    def test_backfills_sha256_for_existing_unhashed_file(self, fetcher, tmp_path, mock_db):
        body = b"already-on-disk-firmware"
        (tmp_path / self.BETA).write_bytes(body)
        db.register_firmware(self.BETA, source="auto")  # pre-feature: no hash
        assert db.get_firmware_sha256(self.BETA) is None

        rel = self._release()
        with patch.object(fetcher, "_scrape_page",
                          AsyncMock(side_effect=self._scrape_only_30x(rel))):
            asyncio.run(fetcher.check_and_download())

        assert db.get_firmware_sha256(self.BETA) == hashlib.sha256(body).hexdigest()

    def test_existing_hash_survives_recheck(self, fetcher, tmp_path, mock_db):
        # The 24h re-register of an already-present file must not erase its hash
        # (regression for the ON CONFLICT NULL-overwrite; needs the COALESCE fix).
        (tmp_path / self.BETA).write_bytes(b"already-on-disk-firmware")
        db.register_firmware(self.BETA, source="auto", sha256="preserve-me")

        rel = self._release()
        with patch.object(fetcher, "_scrape_page",
                          AsyncMock(side_effect=self._scrape_only_30x(rel))):
            asyncio.run(fetcher.check_and_download())

        assert db.get_firmware_sha256(self.BETA) == "preserve-me"
