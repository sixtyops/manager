"""Auto-fetch firmware from Tachyon Networks Freshdesk release pages."""

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Optional

import httpx

from . import database as db
from .firmware_policy import (
    MIN_PLAUSIBLE_SIZE_FRACTION,
    PLATFORM_SETTING_KEYS,
    auto_select_platform_target,
    detect_platform,
    pin_setting_key,
)

logger = logging.getLogger(__name__)

# Global singleton
_fetcher: Optional["FirmwareFetcher"] = None

FRESHDESK_PAGES = {
    "tna-30x": "https://tachyon-networks.freshdesk.com/support/solutions/articles/67000710575-tna-300-series-firmware-releases",
    "tna-303l": "https://tachyon-networks.freshdesk.com/support/solutions/articles/67000745898-tna-303l-firmware-releases",
    "tns-100": "https://tachyon-networks.freshdesk.com/support/solutions/articles/67000719270-tns-100-firmware-releases",
}
FIRMWARE_INDEX_URL = "https://tachyon-networks.freshdesk.com/support/solutions/articles/67000714399"


class _FamilyIndexParser(HTMLParser):
    """Read family rows from Tachyon's public firmware index table."""

    def __init__(self):
        super().__init__()
        self.rows: list[tuple[str, str]] = []
        self._table_depth = 0
        self._row_text: list[str] | None = None
        self._row_url: str | None = None
        self._outside_url: str | None = None
        self._outside_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            self._table_depth += 1
        elif tag == "tr" and self._table_depth:
            self._row_text = []
            self._row_url = None
        elif tag == "a" and self._row_text is not None:
            href = attrs.get("href", "")
            if "solutions/articles/" in href:
                self._row_url = href
        elif tag == "a" and not self._table_depth:
            href = attrs.get("href", "")
            if "solutions/articles/" in href:
                self._outside_url = href
                self._outside_text = []

    def handle_endtag(self, tag):
        if tag == "a":
            if self._outside_url:
                label = " ".join(self._outside_text)
                if re.search(r"TNA|TNS", label, re.IGNORECASE):
                    self.rows.append((label, self._outside_url))
                self._outside_url = None
                self._outside_text = []
        elif tag == "tr" and self._row_text is not None:
            if self._row_url:
                self.rows.append((" ".join(self._row_text), self._row_url))
            self._row_text = None
            self._row_url = None
        elif tag == "table" and self._table_depth:
            self._table_depth -= 1

    def handle_data(self, data):
        if self._row_text is not None and data.strip():
            self._row_text.append(data.strip())
        elif self._outside_url and data.strip():
            self._outside_text.append(data.strip())


def _platform_for_family_row(text: str) -> Optional[str]:
    """Map a vendor family row to a Manager-supported firmware platform."""
    normalized = text.upper()
    if "TNA-303L" in normalized:
        return "tna-303l"
    if any(model in normalized for model in ("TNA-301", "TNA-302", "TNA-303X")):
        return "tna-30x"
    if "TNS-100" in normalized:
        return "tns-100"
    return None


def _discover_family_pages(html: str) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Return supported (platform, url, family row) entries and parse errors."""
    parser = _FamilyIndexParser()
    parser.feed(html)
    pages = []
    errors = []
    seen_platforms = set()
    for row, url in parser.rows:
        platform = _platform_for_family_row(row)
        if not platform:
            if re.search(r"TNA|TNS", row, re.IGNORECASE):
                errors.append(f"Firmware family is not supported by Manager: {row}")
            continue
        if platform in seen_platforms:
            errors.append(f"Firmware index lists more than one family page for {platform}: {url}")
            continue
        seen_platforms.add(platform)
        if url.startswith("/"):
            if url.startswith("/a/solutions/articles/"):
                url = url.replace("/a/solutions/articles/", "/support/solutions/articles/", 1)
            url = "https://tachyon-networks.freshdesk.com" + url
        pages.append((platform, url, row))
    for platform in PLATFORM_SETTING_KEYS:
        if platform not in seen_platforms:
            errors.append(f"Firmware index has no family page for supported platform {platform}")
    return pages, errors

# Regex: extract "Latest stable" / "Latest beta" version from the summary table.
# Handles inline <span> tags and &nbsp; in the version cell, and tolerates a
# stray character between the leading "v" and the first digit (the tna-30x
# beta cell on Freshdesk has been observed rendering as "v.1.15.0 beta-1").
RE_VERSION_TABLE = re.compile(
    r"<td[^>]*>\s*<strong>\s*(Latest\s+(?:stable|beta))\s*</strong>\s*</td>"
    r"\s*<td[^>]*>(?:<[^>]+>|\s|&nbsp;)*v[^\d<]*(\d+(?:\.\d+)*)",
    re.IGNORECASE,
)


def _normalize_version(v: str) -> str:
    """Canonicalise a version string captured from KB HTML.

    Strips whitespace and any leading/trailing dots so that "v.1.15.0",
    ".1.15.0", and "1.15.0 " all compare equal to "1.15.0". Keeps only
    the first whitespace-delimited token to drop any "beta-1" suffix.
    """
    v = v.strip().strip(".")
    return v.split()[0] if v else v

# Regex: extract download URL + version from <a> tags
# Handles <strong> wrapper around "Version X.Y.Z" text
RE_DOWNLOAD_LINK = re.compile(
    r'<a\s+[^>]*href="(https://tachyon-networks\.com/fw/[^"]+\.bin)"[^>]*>'
    r"(?:\s|<[^>]+>)*Version\s+([\d.]+)",
    re.IGNORECASE,
)

# Regex: pair a release version to its vendor-published MD5 within one
# summary-table row. The Freshdesk summary table gained an MD5 column in
# 2026-06 (columns: Type | File | Release date | MD5); the version lives in
# the File cell's <a> text (e.g. "v1.12.3", "v.1.15.0") and the MD5 is a bare
# 32-hex token a couple of cells later. The tempered dot `(?!</tr>)` keeps the
# match inside a single <tr> so one row's hash can't bind to another row's
# version. Always run this over <table> regions only (see _parse_table_md5s):
# the release-notes body also contains inline "MD5: <hash>" lines that would
# otherwise pair a hash to the wrong version.
RE_TABLE_MD5 = re.compile(
    r"v[^\d<]*(\d+(?:\.\d+)*)"      # version in the File cell
    r"(?:(?!</tr>).)*?"            # ... same row only ...
    r"\b([0-9a-fA-F]{32})\b",      # the MD5 cell
    re.IGNORECASE | re.DOTALL,
)

CHECK_INTERVAL = 86400  # 24 hours


def _parse_table_md5s(html: str) -> dict[str, str]:
    """Compatibility helper that returns table-published MD5 values."""
    md5s: dict[str, str] = {}
    for table in re.findall(r"<table.*?</table>", html, re.DOTALL):
        for version, md5 in RE_TABLE_MD5.findall(table):
            md5s.setdefault(_normalize_version(version), md5.lower())
    return md5s


def _checksum_fields(text: str) -> dict[str, str]:
    """Extract labeled vendor checksums from an HTML fragment."""
    plain = unescape(re.sub(r"<[^>]+>", " ", text))
    fields: dict[str, str] = {}
    for label, value in re.findall(
        r"\b(SHA\s*-?\s*256|MD5)\b\s*:?\s*([0-9a-fA-F]{32,64})", plain, re.I
    ):
        algorithm = "sha256" if "SHA" in label.upper() else "md5"
        if len(value) == (64 if algorithm == "sha256" else 32):
            fields[algorithm] = value.lower()
    # Current summary tables label the MD5 column but leave row values bare.
    if "MD5" in plain.upper():
        fields.setdefault("md5", next(
            (value.lower() for value in re.findall(r"\b[0-9a-fA-F]{32}\b", plain)),
            "",
        ))
        if not fields["md5"]:
            fields.pop("md5")
    if "SHA-256" in plain.upper() or "SHA256" in plain.upper():
        fields.setdefault("sha256", next(
            (value.lower() for value in re.findall(r"\b[0-9a-fA-F]{64}\b", plain)),
            "",
        ))
        if not fields["sha256"]:
            fields.pop("sha256")
    if not fields:
        for value in re.findall(r"\b[0-9a-fA-F]{64}\b", plain):
            fields["sha256"] = value.lower()
        for value in re.findall(r"\b[0-9a-fA-F]{32}\b", plain):
            fields["md5"] = value.lower()
    return fields


def _parse_table_checksums(html: str) -> dict[str, dict[str, str]]:
    """Map firmware versions to MD5 or SHA-256 values in summary-table rows."""
    checksums: dict[str, dict[str, str]] = {}
    for table in re.findall(r"<table.*?</table>", html, re.DOTALL | re.IGNORECASE):
        for row in re.findall(r"<tr\b.*?</tr>", table, re.DOTALL | re.IGNORECASE):
            version = re.search(r"v[^\d<]*(\d+(?:\.\d+)+)", row, re.I)
            if version:
                fields = _checksum_fields(row)
                if fields:
                    checksums.setdefault(_normalize_version(version.group(1)), {}).update(fields)
    return checksums


def _parse_release_note_checksums(html: str) -> dict[str, dict[str, str]]:
    """Map checksums following Version download links to their release version."""
    matches = list(RE_DOWNLOAD_LINK.finditer(html))
    checksums: dict[str, dict[str, str]] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(html)
        fields = _checksum_fields(html[match.end():end])
        if fields:
            checksums.setdefault(_normalize_version(match.group(2)), {}).update(fields)
    return checksums


@dataclass
class FirmwareRelease:
    platform: str       # "tna-30x" or "tna-303l"
    version: str        # e.g. "1.12.3"
    download_url: str
    channel: str        # "stable" or "beta"
    filename: str       # basename of the URL
    md5: Optional[str] = None  # vendor-published MD5 (lowercase), or None if not listed
    sha256: Optional[str] = None  # vendor-published SHA-256, preferred when listed


class FirmwareFetcher:
    """Background service that fetches firmware from Tachyon's release pages."""

    def __init__(self, firmware_dir: Path, broadcast_func: Callable,
                 check_interval: int = CHECK_INTERVAL):
        self.firmware_dir = firmware_dir
        self.broadcast_func = broadcast_func
        self.check_interval = check_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._download_warning: Optional[str] = None

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._check_loop())
        logger.info("Firmware fetcher started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Firmware fetcher stopped")

    async def _check_loop(self):
        # Delay startup check to let DNS/network settle, retry once on failure
        await asyncio.sleep(15)
        try:
            await self.check_and_download()
        except Exception as e:
            logger.warning(f"Firmware fetch failed on startup, retrying in 30s: {e}")
            await asyncio.sleep(30)
            try:
                await self.check_and_download()
            except Exception as e2:
                logger.exception(f"Firmware fetch retry also failed: {e2}")

        while self._running:
            await asyncio.sleep(self.check_interval)
            try:
                await self.check_and_download()
            except Exception as e:
                logger.exception(f"Firmware fetch error: {e}")

    async def check_and_download(self) -> dict:
        """Scrape release pages, download missing firmware, auto-select.

        Returns summary dict with 'releases' and 'downloaded' lists.
        """
        all_releases = []
        downloaded = []
        replaced = []  # Track replaced files
        errors = []

        beta_enabled = db.get_setting("firmware_beta_enabled", "false") == "true"
        auto_fetched = self._get_auto_fetched_list()
        channel_map = self._get_channel_map()

        family_pages, index_errors = await self._discover_pages()
        errors.extend(index_errors)

        for platform, url, family in family_pages:
            try:
                releases, warnings = await self._scrape_page(platform, url)
                all_releases.extend(releases)
                errors.extend(f"{family}: {warning}" for warning in warnings)
            except Exception as e:
                # Retry once after a brief delay
                logger.warning(f"Scrape family {family} failed, retrying: {e}")
                await asyncio.sleep(5)
                try:
                    releases, warnings = await self._scrape_page(platform, url)
                    all_releases.extend(releases)
                    errors.extend(f"{family}: {warning}" for warning in warnings)
                except Exception as e2:
                    msg = f"Failed to scrape firmware family {family} ({url}): {e2}"
                    logger.error(msg)
                    errors.append(msg)
                    continue

            for release in releases:
                if not (release.sha256 or release.md5):
                    msg = (
                        f"{family}: no published MD5 or SHA-256 for "
                        f"{release.filename}; firmware was not registered"
                    )
                    logger.warning(msg)
                    errors.append(msg)
                    continue
                filepath = self.firmware_dir / release.filename
                if filepath.exists():
                    # Track as auto-fetched even if already present
                    if release.filename not in auto_fetched:
                        auto_fetched.append(release.filename)
                    # Re-check an on-disk image against Tachyon's published
                    # checksum before keeping its local pre-flash fingerprint.
                    # On mismatch, discard it and download a verified copy.
                    file_md5, file_sha256 = await self._hash_existing_file(filepath)
                    actual_vendor_hash = file_sha256 if release.sha256 else file_md5
                    expected_vendor_hash = release.sha256 or release.md5
                    if (
                        not actual_vendor_hash
                        or actual_vendor_hash.lower() != expected_vendor_hash.lower()
                    ):
                        logger.error(
                            f"On-disk {release.filename} fails the vendor checksum "
                            f"(expected {expected_vendor_hash}, got {actual_vendor_hash}) — discarding "
                            "and re-downloading"
                        )
                        filepath.unlink(missing_ok=True)
                        # fall through to the download path below (no continue)
                    else:
                        db.register_firmware(
                            release.filename, source="auto", sha256=file_sha256,
                        )
                        continue

                success, sha256 = await self._download_firmware(release)
                if success:
                    downloaded.append(release.filename)
                    auto_fetched.append(release.filename)
                    db.register_firmware(release.filename, source="auto", sha256=sha256)

                    # Replace older auto-fetched firmware of same platform/channel
                    old_files = self._find_old_firmware(
                        platform, release.channel, release.filename,
                        auto_fetched, channel_map
                    )
                    for old_file in old_files:
                        old_path = self.firmware_dir / old_file
                        if old_path.exists():
                            old_path.unlink()
                            logger.info(f"Replaced old firmware: {old_file} -> {release.filename}")
                        if old_file in auto_fetched:
                            auto_fetched.remove(old_file)
                        db.unregister_firmware(old_file)
                        replaced.append(old_file)
                else:
                    errors.append(
                        self._download_warning
                        or f"Download failed checksum validation: {release.filename}"
                    )

            # Auto-select for this platform
            self._auto_select(platform, releases, beta_enabled)

        if not family_pages and not errors:
            errors.append(f"No supported firmware family pages found at {FIRMWARE_INDEX_URL}")

        # Persist channel metadata (filename -> "stable"/"beta")
        channel_map = self._get_channel_map()
        for r in all_releases:
            channel_map[r.filename] = r.channel
        self._save_channel_map(channel_map)

        # Persist state
        self._save_auto_fetched_list(auto_fetched)
        db.set_setting("firmware_last_check", datetime.now().isoformat())
        if errors:
            db.set_setting("firmware_last_check_error", "; ".join(errors))
        else:
            db.set_setting("firmware_last_check_error", "")

        # Broadcast update — also fires on errors-only so a future
        # UI listener can refresh the status line on parse warnings,
        # not just on completed downloads.
        if self.broadcast_func and (downloaded or replaced or errors):
            await self.broadcast_func({
                "type": "firmware_fetched",
                "downloaded": downloaded,
                "replaced": replaced,
                "errors": errors,
            })

        summary = {
            "releases": [
                {"platform": r.platform, "version": r.version,
                 "channel": r.channel, "filename": r.filename}
                for r in all_releases
            ],
            "downloaded": downloaded,
            "replaced": replaced,
            "errors": errors,
        }
        logger.info(f"Firmware check complete: {len(all_releases)} releases found, "
                     f"{len(downloaded)} downloaded, {len(replaced)} replaced")
        return summary

    async def _scrape_page(
        self, platform: str, url: str
    ) -> tuple[list[FirmwareRelease], list[str]]:
        """Fetch a Freshdesk article page and parse firmware releases.

        Returns the parsed releases plus a list of per-platform parse
        warnings — one entry for each channel the page's summary table
        promised that no download link ultimately matched. Treat these
        like soft errors: surface to the operator, do not retry.
        """
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text

        # Parse the version summary table
        table_matches = RE_VERSION_TABLE.findall(html)
        stable_version = None
        beta_version = None
        for label, version in table_matches:
            version = _normalize_version(version)
            if "stable" in label.lower():
                stable_version = version
            elif "beta" in label.lower():
                beta_version = version

        # Parse download links
        link_matches = RE_DOWNLOAD_LINK.findall(html)

        # Summary-table checksums take precedence. Release notes carry hashes on
        # pages such as TNS-100, which do not have a summary table.
        checksums_by_version = _parse_release_note_checksums(html)
        for version, fields in _parse_table_checksums(html).items():
            checksums_by_version.setdefault(version, {}).update(fields)

        releases = []
        has_version_table = stable_version is not None or beta_version is not None

        for download_url, version in link_matches:
            version = _normalize_version(version)
            if has_version_table:
                # Pages with a summary table: only grab stable/beta
                if version == stable_version:
                    channel = "stable"
                elif version == beta_version:
                    channel = "beta"
                else:
                    continue  # skip older versions
            else:
                # Pages without a summary table (e.g. TNS-100): treat the
                # first (newest) link as stable, skip the rest.
                if not releases:
                    channel = "stable"
                else:
                    break

            filename = download_url.rsplit("/", 1)[-1]
            releases.append(FirmwareRelease(
                platform=platform,
                version=version,
                download_url=download_url,
                channel=channel,
                filename=filename,
                md5=checksums_by_version.get(version, {}).get("md5"),
                sha256=checksums_by_version.get(version, {}).get("sha256"),
            ))

        # Parse-sanity guard: if the summary table promised a release we
        # never matched a download link for, surface a warning so the
        # next vendor-side HTML drift is loud instead of silent. Only
        # applies to pages that *have* a summary table.
        warnings: list[str] = []
        seen_channels = {r.channel for r in releases}
        if stable_version and "stable" not in seen_channels:
            warnings.append(
                f"{platform}: summary table lists stable v{stable_version} "
                f"but no matching download link was found"
            )
        if beta_version and "beta" not in seen_channels:
            warnings.append(
                f"{platform}: summary table lists beta v{beta_version} "
                f"but no matching download link was found"
            )

        if not releases and not warnings:
            warnings.append(
                f"{platform}: no stable or beta firmware download could be parsed from {url}"
            )
        for w in warnings:
            logger.warning(w)

        return releases, warnings

    async def _discover_pages(self) -> tuple[list[tuple[str, str, str]], list[str]]:
        """Fetch Tachyon's public index and map its family rows to supported platforms."""
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.get(FIRMWARE_INDEX_URL)
                response.raise_for_status()
            pages, errors = _discover_family_pages(response.text)
            return pages, errors
        except Exception as e:
            return [], [f"Failed to parse Tachyon firmware family index {FIRMWARE_INDEX_URL}: {e}"]

    def _suspect_truncated_size(self, filename: str, size_bytes: int) -> Optional[str]:
        """Return a reason string if `size_bytes` is implausibly small versus
        other on-disk firmware of the same platform, else None.

        Catches truncated downloads the server didn't advertise (no/!chunked
        Content-Length) — e.g. a 6.7 MB image when the platform's other
        firmware is ~18 MB. Returns None when there's no same-platform sibling
        to compare against (first download for a platform); Content-Length is
        the only guard there.
        """
        platform = _detect_platform(filename)
        sibling_max = 0
        for p in self.firmware_dir.iterdir():
            if not p.is_file() or p.name == filename:
                continue
            if _detect_platform(p.name) != platform:
                continue
            try:
                sibling_max = max(sibling_max, p.stat().st_size)
            except OSError:
                continue
        if sibling_max and size_bytes < sibling_max * MIN_PLAUSIBLE_SIZE_FRACTION:
            return (
                f"{filename} is {size_bytes / 1048576:.1f} MB, far smaller than the "
                f"{platform} firmware already on disk ({sibling_max / 1048576:.1f} MB)"
            )
        return None

    async def _download_firmware(self, release: FirmwareRelease) -> tuple[bool, Optional[str]]:
        """Download a firmware .bin file with streaming.

        Verifies the download before accepting it: a transfer whose preferred
        vendor SHA-256 or fallback MD5 doesn't match, that is shorter than its advertised
        Content-Length, or that is far smaller than the platform's other
        firmware is rejected rather than renamed into place — so a corrupt or
        partial image can never become the auto-selected target and get flashed
        to a device. The MD5 check is fail-open: when the release page lists no
        MD5 (`release.md5` is None) the size guards still apply. See issue #214.

        Returns (True, sha256_hex) on success — the SHA256 is computed in the
        same streaming pass so the caller can store it, bringing auto-fetched
        firmware under the same pre-flash integrity re-check as manual uploads.
        Returns (False, None) on any rejection or error.
        """
        filepath = self.firmware_dir / release.filename
        tmp_path = filepath.with_suffix(".downloading")
        self._download_warning = None

        if not (release.sha256 or release.md5):
            self._download_warning = f"No published checksum for {release.filename}"
            logger.warning(self._download_warning)
            return False, None

        logger.info(f"Downloading {release.filename} from {release.download_url}")
        try:
            expected_len = None
            bytes_written = 0
            md5_hash = hashlib.md5()
            sha256_hash = hashlib.sha256()
            async with httpx.AsyncClient(timeout=600, follow_redirects=True) as client:
                async with client.stream("GET", release.download_url) as resp:
                    resp.raise_for_status()
                    cl = resp.headers.get("content-length")
                    if cl and cl.isdigit():
                        expected_len = int(cl)
                    with open(tmp_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            f.write(chunk)
                            bytes_written += len(chunk)
                            md5_hash.update(chunk)
                            sha256_hash.update(chunk)

            # Prefer SHA-256 when Tachyon publishes it.
            expected_checksum = release.sha256 or release.md5
            actual_checksum = (
                sha256_hash.hexdigest() if release.sha256 else md5_hash.hexdigest()
            )
            if actual_checksum.lower() != expected_checksum.lower():
                self._download_warning = (
                    f"Published checksum mismatch for {release.filename}: "
                    f"expected {expected_checksum}, got {actual_checksum}"
                )
                tmp_path.unlink(missing_ok=True)
                logger.error(self._download_warning)
                return False, None

            # Reject a server-advertised size mismatch (truncated transfer).
            if expected_len is not None and bytes_written != expected_len:
                tmp_path.unlink(missing_ok=True)
                self._download_warning = (
                    f"Download rejected for {release.filename}: received {bytes_written} "
                    f"bytes, expected {expected_len} from Content-Length"
                )
                logger.error(self._download_warning)
                return False, None

            # Reject a size outlier vs the platform's other firmware (catches
            # truncations the server didn't advertise).
            suspect = self._suspect_truncated_size(release.filename, bytes_written)
            if suspect:
                tmp_path.unlink(missing_ok=True)
                self._download_warning = f"Download rejected: {suspect}"
                logger.error(self._download_warning)
                return False, None

            tmp_path.rename(filepath)
            logger.info(f"Downloaded {release.filename} ({bytes_written / 1048576:.1f} MB)")
            return True, sha256_hash.hexdigest()

        except Exception as e:
            self._download_warning = f"Failed to download {release.filename}: {e}"
            logger.error(self._download_warning)
            if tmp_path.exists():
                tmp_path.unlink()
            return False, None

    async def _hash_existing_file(
        self, filepath: Path
    ) -> tuple[Optional[str], Optional[str]]:
        """Return (md5, sha256) for a file already on disk, off the event loop.

        Mirrors the pre-flash integrity hash in app.py (1 MB blocks in an
        executor), computing both digests in a single read pass: the MD5 lets
        the caller re-verify an existing file against the vendor checksum before
        trusting it, and the SHA256 is what the pre-flash check stores. Returns
        (None, None) if the file vanished mid-read (e.g. operator cleanup) so
        the caller proceeds without crashing the check loop.
        """
        def _hash() -> tuple[str, str]:
            md5 = hashlib.md5()
            sha256 = hashlib.sha256()
            with open(filepath, "rb") as fh:
                for block in iter(lambda: fh.read(1024 * 1024), b""):
                    md5.update(block)
                    sha256.update(block)
            return md5.hexdigest(), sha256.hexdigest()
        try:
            return await asyncio.get_event_loop().run_in_executor(None, _hash)
        except FileNotFoundError:
            return None, None

    def _auto_select(self, platform: str, releases: list[FirmwareRelease],
                     beta_enabled: bool):
        """Auto-select the best firmware for a platform.

        Skips platforms the operator has pinned to a specific version, so a
        manual choice (e.g. holding a known-good stable, or avoiding a beta) is
        never overwritten by auto-tracking. The one exception is uptime safety:
        if a pinned file has gone missing on disk we clear the pin and re-derive
        a target rather than leave the scheduler pointing at a file that isn't
        there.
        """
        auto_select_platform_target(
            platform,
            self.firmware_dir,
            beta_enabled,
            releases=releases,
        )

    def _get_auto_fetched_list(self) -> list[str]:
        raw = db.get_setting("firmware_auto_fetched_files", "")
        if not raw:
            return []
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []

    def _save_auto_fetched_list(self, files: list[str]):
        # Deduplicate
        unique = list(dict.fromkeys(files))
        db.set_setting("firmware_auto_fetched_files", json.dumps(unique))

    def _get_channel_map(self) -> dict[str, str]:
        raw = db.get_setting("firmware_channels", "")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}

    def _save_channel_map(self, channel_map: dict[str, str]):
        db.set_setting("firmware_channels", json.dumps(channel_map))

    def _find_old_firmware(self, platform: str, channel: str, new_filename: str,
                           auto_fetched: list[str], channel_map: dict[str, str]) -> list[str]:
        """Find older auto-fetched firmware files of the same platform/channel to replace."""
        old_files = []
        for filename in auto_fetched:
            if filename == new_filename:
                continue
            # Check same platform
            if _detect_platform(filename) != platform:
                continue
            # Check same channel
            file_channel = channel_map.get(filename, "")
            if file_channel != channel:
                continue
            # This is an older auto-fetched file of the same platform/channel
            old_files.append(filename)
        return old_files

    def reselect(self, beta_enabled: bool):
        """Re-run auto-select for all platforms using cached release data."""
        channel_map = self._get_channel_map()
        for platform in FRESHDESK_PAGES:
            # Reconstruct minimal release objects from channel map + local files
            releases = []
            for filename, channel in channel_map.items():
                detected = _detect_platform(filename)
                if detected == platform:
                    releases.append(FirmwareRelease(
                        platform=platform,
                        version="",
                        download_url="",
                        channel=channel,
                        filename=filename,
                    ))
            if releases:
                self._auto_select(platform, releases, beta_enabled)


def _detect_platform(filename: str) -> str:
    """Detect firmware platform from filename."""
    return detect_platform(filename)


def get_fetcher() -> Optional[FirmwareFetcher]:
    return _fetcher


def init_fetcher(firmware_dir: Path, broadcast_func: Callable,
                 check_interval: int = CHECK_INTERVAL) -> FirmwareFetcher:
    global _fetcher
    _fetcher = FirmwareFetcher(firmware_dir, broadcast_func, check_interval)
    return _fetcher
