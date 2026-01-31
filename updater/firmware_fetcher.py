"""Auto-fetch firmware from Tachyon Networks Freshdesk release pages."""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import httpx

from . import database as db

logger = logging.getLogger(__name__)

# Global singleton
_fetcher: Optional["FirmwareFetcher"] = None

FRESHDESK_PAGES = {
    "tna-30x": "https://tachyon-networks.freshdesk.com/support/solutions/articles/67000710575-tna-300-series-firmware-releases",
    "tna-303l": "https://tachyon-networks.freshdesk.com/support/solutions/articles/67000745898-tna-303l-firmware-releases",
}

# Regex: extract "Latest stable" / "Latest beta" version from the summary table
# Handles inline <span> tags and &nbsp; in the version cell
RE_VERSION_TABLE = re.compile(
    r"<td[^>]*>\s*<strong>\s*(Latest\s+(?:stable|beta))\s*</strong>\s*</td>"
    r"\s*<td[^>]*>(?:<[^>]+>|\s|&nbsp;)*v([\d.]+)",
    re.IGNORECASE,
)

# Regex: extract download URL + version from <a> tags
# Handles <strong> wrapper around "Version X.Y.Z" text
RE_DOWNLOAD_LINK = re.compile(
    r'<a\s+[^>]*href="(https://tachyon-networks\.com/fw/[^"]+\.bin)"[^>]*>'
    r"(?:\s|<[^>]+>)*Version\s+([\d.]+)",
    re.IGNORECASE,
)

CHECK_INTERVAL = 86400  # 24 hours


@dataclass
class FirmwareRelease:
    platform: str       # "tna-30x" or "tna-303l"
    version: str        # e.g. "1.12.3"
    download_url: str
    channel: str        # "stable" or "beta"
    filename: str       # basename of the URL


class FirmwareFetcher:
    """Background service that fetches firmware from Tachyon's release pages."""

    def __init__(self, firmware_dir: Path, broadcast_func: Callable,
                 check_interval: int = CHECK_INTERVAL):
        self.firmware_dir = firmware_dir
        self.broadcast_func = broadcast_func
        self.check_interval = check_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None

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
        # Immediate check on startup
        try:
            await self.check_and_download()
        except Exception as e:
            logger.exception(f"Firmware fetch error on startup: {e}")

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
        errors = []

        beta_enabled = db.get_setting("firmware_beta_enabled", "false") == "true"
        auto_fetched = self._get_auto_fetched_list()

        for platform, url in FRESHDESK_PAGES.items():
            try:
                releases = await self._scrape_page(platform, url)
                all_releases.extend(releases)
            except Exception as e:
                msg = f"Failed to scrape {platform}: {e}"
                logger.error(msg)
                errors.append(msg)
                continue

            for release in releases:
                filepath = self.firmware_dir / release.filename
                if filepath.exists():
                    # Track as auto-fetched even if already present
                    if release.filename not in auto_fetched:
                        auto_fetched.append(release.filename)
                    continue

                success = await self._download_firmware(release)
                if success:
                    downloaded.append(release.filename)
                    auto_fetched.append(release.filename)
                else:
                    errors.append(f"Download failed: {release.filename}")

            # Auto-select for this platform
            self._auto_select(platform, releases, beta_enabled)

        # Persist state
        self._save_auto_fetched_list(auto_fetched)
        db.set_setting("firmware_last_check", datetime.now().isoformat())
        if errors:
            db.set_setting("firmware_last_check_error", "; ".join(errors))
        else:
            db.set_setting("firmware_last_check_error", "")

        # Broadcast update
        if self.broadcast_func and downloaded:
            await self.broadcast_func({
                "type": "firmware_fetched",
                "downloaded": downloaded,
            })

        summary = {
            "releases": [
                {"platform": r.platform, "version": r.version,
                 "channel": r.channel, "filename": r.filename}
                for r in all_releases
            ],
            "downloaded": downloaded,
            "errors": errors,
        }
        logger.info(f"Firmware check complete: {len(all_releases)} releases found, "
                     f"{len(downloaded)} downloaded")
        return summary

    async def _scrape_page(self, platform: str, url: str) -> list[FirmwareRelease]:
        """Fetch a Freshdesk article page and parse firmware releases."""
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text

        # Parse the version summary table
        table_matches = RE_VERSION_TABLE.findall(html)
        stable_version = None
        beta_version = None
        for label, version in table_matches:
            version = version.split()[0]  # strip "r7781" suffix if present
            if "stable" in label.lower():
                stable_version = version
            elif "beta" in label.lower():
                beta_version = version

        # Parse download links
        link_matches = RE_DOWNLOAD_LINK.findall(html)

        releases = []
        for download_url, version in link_matches:
            channel = "unknown"
            if version == stable_version:
                channel = "stable"
            elif version == beta_version:
                channel = "beta"
            else:
                continue  # skip older versions

            filename = download_url.rsplit("/", 1)[-1]
            releases.append(FirmwareRelease(
                platform=platform,
                version=version,
                download_url=download_url,
                channel=channel,
                filename=filename,
            ))

        if not releases:
            logger.warning(f"No firmware releases found on {url}")

        return releases

    async def _download_firmware(self, release: FirmwareRelease) -> bool:
        """Download a firmware .bin file with streaming."""
        filepath = self.firmware_dir / release.filename
        tmp_path = filepath.with_suffix(".downloading")

        logger.info(f"Downloading {release.filename} from {release.download_url}")
        try:
            async with httpx.AsyncClient(timeout=600, follow_redirects=True) as client:
                async with client.stream("GET", release.download_url) as resp:
                    resp.raise_for_status()
                    with open(tmp_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            f.write(chunk)

            tmp_path.rename(filepath)
            size_mb = filepath.stat().st_size / (1024 * 1024)
            logger.info(f"Downloaded {release.filename} ({size_mb:.1f} MB)")
            return True

        except Exception as e:
            logger.error(f"Failed to download {release.filename}: {e}")
            if tmp_path.exists():
                tmp_path.unlink()
            return False

    def _auto_select(self, platform: str, releases: list[FirmwareRelease],
                     beta_enabled: bool):
        """Auto-select the best firmware for a platform."""
        setting_key = "selected_firmware_30x" if platform == "tna-30x" else "selected_firmware_303l"

        # Prefer beta if enabled, otherwise stable
        best = None
        for r in releases:
            if r.channel == "beta" and beta_enabled:
                filepath = self.firmware_dir / r.filename
                if filepath.exists():
                    best = r.filename
                    break
            elif r.channel == "stable":
                filepath = self.firmware_dir / r.filename
                if filepath.exists():
                    if not best:
                        best = r.filename

        if best:
            current = db.get_setting(setting_key, "")
            if current != best:
                db.set_setting(setting_key, best)
                logger.info(f"Auto-selected {setting_key} = {best}")

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


def get_fetcher() -> Optional[FirmwareFetcher]:
    return _fetcher


def init_fetcher(firmware_dir: Path, broadcast_func: Callable,
                 check_interval: int = CHECK_INTERVAL) -> FirmwareFetcher:
    global _fetcher
    _fetcher = FirmwareFetcher(firmware_dir, broadcast_func, check_interval)
    return _fetcher
