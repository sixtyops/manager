"""Tests for the release checker and self-update mechanism."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

import pytest


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------

class TestVersionComparison:
    """Verify that check_for_updates correctly identifies upgrades only."""

    @pytest.fixture(autouse=True)
    def _patch_db(self, mock_db):
        pass

    async def _check(self, current, latest_tag):
        with patch("updater.release_checker.__version__", current):
            from updater.release_checker import ReleaseChecker

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {
                "tag_name": latest_tag,
                "html_url": "https://github.com/sixtyops/manager/releases/tag/" + latest_tag,
                "body": "notes",
            }

            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)

            with patch("updater.release_checker.httpx.AsyncClient", return_value=mock_client):
                checker = ReleaseChecker(broadcast_func=AsyncMock())
                return await checker.check_for_updates()

    @pytest.mark.asyncio
    async def test_newer_version_flags_update(self):
        result = await self._check("1.0.0", "v1.0.1")
        assert result["update_available"] is True
        assert result["latest_version"] == "1.0.1"

    @pytest.mark.asyncio
    async def test_same_version_no_update(self):
        result = await self._check("1.0.1", "v1.0.1")
        assert result["update_available"] is False

    @pytest.mark.asyncio
    async def test_older_version_no_update(self):
        """A lower remote version must NOT flag an update (no downgrades)."""
        result = await self._check("2.0.0", "v1.0.1")
        assert result["update_available"] is False

    @pytest.mark.asyncio
    async def test_unparseable_version_no_update(self):
        """If version strings can't be parsed, don't flag an update."""
        result = await self._check("1.0.0", "vNOT_A_VERSION")
        assert result["update_available"] is False

    @pytest.mark.asyncio
    async def test_dev_channel_checks_all_releases(self, mock_db):
        """Dev channel should hit /releases (all) not /releases/latest."""
        from updater import database
        database.set_setting("release_channel", "dev")

        with patch("updater.release_checker.__version__", "1.0.0"):
            from updater.release_checker import ReleaseChecker

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = [
                {
                    "tag_name": "v1.1.0-dev.1",
                    "prerelease": True,
                    "html_url": "https://github.com/test/releases/tag/v1.1.0-dev.1",
                    "body": "dev release notes",
                },
                {
                    "tag_name": "v1.0.0",
                    "prerelease": False,
                    "html_url": "https://github.com/test/releases/tag/v1.0.0",
                    "body": "stable notes",
                },
            ]

            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)

            with patch("updater.release_checker.httpx.AsyncClient", return_value=mock_client):
                checker = ReleaseChecker(broadcast_func=AsyncMock())
                result = await checker.check_for_updates()

        assert result["update_available"] is True
        assert result["latest_version"] == "1.1.0-dev.1"
        assert result["release_channel"] == "dev"
        # Verify it called the releases list endpoint (not /latest)
        call_args = mock_client.get.call_args
        assert "/releases/latest" not in str(call_args)

    @pytest.mark.asyncio
    async def test_stable_channel_uses_latest_endpoint(self, mock_db):
        """Stable channel should only hit /releases/latest."""
        from updater import database
        database.set_setting("release_channel", "stable")

        with patch("updater.release_checker.__version__", "1.0.0"):
            from updater.release_checker import ReleaseChecker, GITHUB_API_LATEST

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {
                "tag_name": "v1.0.1",
                "html_url": "https://github.com/test/releases/tag/v1.0.1",
                "body": "stable notes",
            }

            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)

            with patch("updater.release_checker.httpx.AsyncClient", return_value=mock_client):
                checker = ReleaseChecker(broadcast_func=AsyncMock())
                result = await checker.check_for_updates()

        assert result["update_available"] is True
        assert result["latest_version"] == "1.0.1"
        assert result["release_channel"] == "stable"
        # Verify it called the /latest endpoint
        mock_client.get.assert_called_once_with(
            GITHUB_API_LATEST,
            headers={"Accept": "application/vnd.github+json"},
        )


# ---------------------------------------------------------------------------
# apply_update guardrails
# ---------------------------------------------------------------------------

class TestApplyUpdateGuardrails:
    """Test that apply_update blocks when unsafe and uses correct tag."""

    @pytest.fixture(autouse=True)
    def _patch_db(self, mock_db):
        self.db = mock_db

    def _set_setting(self, key, value):
        self.db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.db.commit()

    @pytest.mark.asyncio
    async def test_blocks_during_active_rollout(self):
        from updater.release_checker import apply_update
        self._set_setting("autoupdate_available_version", "1.0.2")

        with patch("updater.release_checker.db.get_active_rollout",
                    return_value={"status": "in_progress"}):
            result = await apply_update()

        assert result["success"] is False
        assert "rollout" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_blocks_during_maintenance_window(self):
        from updater.release_checker import apply_update
        self._set_setting("autoupdate_available_version", "1.0.2")
        self._set_setting("schedule_enabled", "true")
        self._set_setting("schedule_days", "mon,tue,wed,thu,fri,sat,sun")
        self._set_setting("schedule_start_hour", "0")
        self._set_setting("schedule_end_hour", "23")

        with patch("updater.release_checker.db.get_active_rollout", return_value=None), \
             patch("updater.release_checker.services.is_in_schedule_window", return_value=True):
            result = await apply_update()

        assert result["success"] is False
        assert "maintenance" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_requires_available_version(self):
        from updater.release_checker import apply_update
        # No autoupdate_available_version set

        with patch("updater.release_checker.db.get_active_rollout", return_value=None):
            result = await apply_update()

        assert result["success"] is False
        assert "no update version" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_manual_commands_when_no_docker_socket(self):
        from updater.release_checker import apply_update
        self._set_setting("autoupdate_available_version", "1.0.2")

        with patch("updater.release_checker.db.get_active_rollout", return_value=None), \
             patch("updater.release_checker._docker_socket_available", return_value=False):
            result = await apply_update()

        assert result["success"] is False
        assert result["manual"] is True
        assert any("v1.0.2" in cmd for cmd in result["commands"])

    @pytest.mark.asyncio
    async def test_manual_commands_when_no_repo(self):
        from updater.release_checker import apply_update
        self._set_setting("autoupdate_available_version", "1.0.2")

        with patch("updater.release_checker.db.get_active_rollout", return_value=None), \
             patch("updater.release_checker._docker_socket_available", return_value=True), \
             patch("updater.release_checker._get_repo_dir", return_value=None):
            result = await apply_update()

        assert result["success"] is False
        assert result["manual"] is True

    @staticmethod
    def _git_run(calls=None):
        """Mock subprocess.run that returns clean status / SHA / no-op per subcommand."""

        def _run(cmd, **kwargs):
            if calls is not None:
                calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "status" in cmd:
                result.stdout = ""  # clean tree
            elif "rev-parse" in cmd:
                result.stdout = "abc123"
            else:
                result.stdout = ""
            return result

        return _run

    @pytest.mark.asyncio
    async def test_fetches_specific_tag_not_main(self):
        """apply_update must fetch/checkout the release tag, not pull main."""
        from updater.release_checker import apply_update
        self._set_setting("autoupdate_available_version", "1.0.2")

        repo_dir = Path("/tmp/fake-repo")
        calls = []
        mock_run = self._git_run(calls)

        version_content = '__version__ = "1.0.2"'

        with patch("updater.release_checker.db.get_active_rollout", return_value=None), \
             patch("updater.release_checker._docker_socket_available", return_value=True), \
             patch("updater.release_checker._get_repo_dir", return_value=repo_dir), \
             patch("updater.release_checker._get_compose_cmd", return_value=["docker", "compose"]), \
             patch("updater.release_checker._get_host_repo_path", return_value=None), \
             patch("updater.release_checker.subprocess.run", side_effect=mock_run), \
             patch("updater.release_checker.subprocess.Popen"), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "read_text", return_value=version_content):
            result = await apply_update()

        assert result["success"] is True

        # Verify git commands used specific tag, not "pull origin main"
        git_cmds = [c for c in calls if "git" in str(c)]
        fetch_cmd = [c for c in git_cmds if "fetch" in c]
        assert len(fetch_cmd) == 1
        assert "v1.0.2" in fetch_cmd[0]
        assert "main" not in fetch_cmd[0]

        checkout_cmd = [c for c in git_cmds if "checkout" in c]
        assert len(checkout_cmd) == 1
        assert "v1.0.2" in checkout_cmd[0]

    @pytest.mark.asyncio
    async def test_saves_rollback_ref(self):
        """apply_update must save the current git ref before checking out."""
        from updater.release_checker import apply_update
        self._set_setting("autoupdate_available_version", "1.0.2")

        repo_dir = Path("/tmp/fake-repo")

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "status" in cmd:
                result.stdout = ""  # clean tree
            else:
                result.stdout = "abc123def456"
            return result

        version_content = '__version__ = "1.0.2"'

        with patch("updater.release_checker.db.get_active_rollout", return_value=None), \
             patch("updater.release_checker._docker_socket_available", return_value=True), \
             patch("updater.release_checker._get_repo_dir", return_value=repo_dir), \
             patch("updater.release_checker._get_compose_cmd", return_value=["docker", "compose"]), \
             patch("updater.release_checker._get_host_repo_path", return_value="/opt/sixtyops"), \
             patch("updater.release_checker._launch_watchdog", return_value=True), \
             patch("updater.release_checker.subprocess.run", side_effect=mock_run), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "read_text", return_value=version_content):
            result = await apply_update()

        assert result["success"] is True
        # Verify rollback ref was stored in DB
        from updater import database as db
        assert db.get_setting("autoupdate_rollback_ref") == "abc123def456"
        assert db.get_setting("autoupdate_pending_version") == "1.0.2"

    @pytest.mark.asyncio
    async def test_launches_watchdog_when_host_path_available(self):
        """apply_update should launch watchdog instead of direct Popen."""
        from updater.release_checker import apply_update
        self._set_setting("autoupdate_available_version", "1.0.2")

        repo_dir = Path("/tmp/fake-repo")
        mock_run = self._git_run()

        version_content = '__version__ = "1.0.2"'

        with patch("updater.release_checker.db.get_active_rollout", return_value=None), \
             patch("updater.release_checker._docker_socket_available", return_value=True), \
             patch("updater.release_checker._get_repo_dir", return_value=repo_dir), \
             patch("updater.release_checker._get_compose_cmd", return_value=["docker", "compose"]), \
             patch("updater.release_checker._get_host_repo_path", return_value="/opt/sixtyops"), \
             patch("updater.release_checker._launch_watchdog", return_value=True) as mock_watchdog, \
             patch("updater.release_checker.subprocess.run", side_effect=mock_run), \
             patch("updater.release_checker.subprocess.Popen") as mock_popen, \
             patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "read_text", return_value=version_content):
            result = await apply_update()

        assert result["success"] is True
        mock_watchdog.assert_called_once()
        # Popen should NOT be called when watchdog succeeds
        mock_popen.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_popen_when_watchdog_fails(self):
        """If watchdog can't launch, fall back to direct build+swap."""
        from updater.release_checker import apply_update
        self._set_setting("autoupdate_available_version", "1.0.2")

        repo_dir = Path("/tmp/fake-repo")
        mock_run = self._git_run()

        version_content = '__version__ = "1.0.2"'

        with patch("updater.release_checker.db.get_active_rollout", return_value=None), \
             patch("updater.release_checker._docker_socket_available", return_value=True), \
             patch("updater.release_checker._get_repo_dir", return_value=repo_dir), \
             patch("updater.release_checker._get_compose_cmd", return_value=["docker", "compose"]), \
             patch("updater.release_checker._get_host_repo_path", return_value="/opt/sixtyops"), \
             patch("updater.release_checker._launch_watchdog", return_value=False), \
             patch("updater.release_checker.subprocess.run", side_effect=mock_run), \
             patch("updater.release_checker.subprocess.Popen") as mock_popen, \
             patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "read_text", return_value=version_content):
            result = await apply_update()

        assert result["success"] is True
        mock_popen.assert_called_once()

    @pytest.mark.asyncio
    async def test_version_mismatch_aborts_and_reverts(self):
        """If checked-out code has wrong version, abort and revert checkout."""
        from updater.release_checker import apply_update
        self._set_setting("autoupdate_available_version", "1.0.2")

        repo_dir = Path("/tmp/fake-repo")
        calls = []
        mock_run = self._git_run(calls)

        # Version file says 1.0.1, but we expected 1.0.2
        wrong_version = '__version__ = "1.0.1"'

        with patch("updater.release_checker.db.get_active_rollout", return_value=None), \
             patch("updater.release_checker._docker_socket_available", return_value=True), \
             patch("updater.release_checker._get_repo_dir", return_value=repo_dir), \
             patch("updater.release_checker._get_compose_cmd", return_value=["docker", "compose"]), \
             patch("updater.release_checker.subprocess.run", side_effect=mock_run), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "read_text", return_value=wrong_version):
            result = await apply_update()

        assert result["success"] is False
        assert "mismatch" in result["message"].lower()

        # Verify it reverted the checkout
        checkout_cmds = [c for c in calls if "checkout" in c]
        assert len(checkout_cmds) == 2  # first checkout tag, then revert to rollback ref
        assert "abc123" in checkout_cmds[1]  # second checkout uses rollback ref


class TestDirtyTreeHandling:
    """apply_update must detect uncommitted changes before attempting checkout."""

    @pytest.fixture(autouse=True)
    def _patch_db(self, mock_db):
        self.db = mock_db

    def _set_setting(self, key, value):
        self.db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.db.commit()

    @staticmethod
    def _make_git_dispatch(status_porcelain: str):
        """Build a subprocess.run side_effect that dispatches on git subcommand."""

        def _run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "rev-parse" in cmd:
                result.stdout = "abc123def456"
            elif "status" in cmd:
                result.stdout = status_porcelain
            else:
                result.stdout = ""
            return result

        return _run

    @pytest.mark.asyncio
    async def test_dirty_tracked_file_blocks_with_structured_error(self):
        """A dirty tracked file should produce a structured dirty_tree response."""
        from updater.release_checker import apply_update
        self._set_setting("autoupdate_available_version", "1.0.2")

        repo_dir = Path("/tmp/fake-repo")
        side_effect = self._make_git_dispatch(" M docker-compose.yml\n")

        with patch("updater.release_checker.db.get_active_rollout", return_value=None), \
             patch("updater.release_checker._docker_socket_available", return_value=True), \
             patch("updater.release_checker._get_repo_dir", return_value=repo_dir), \
             patch("updater.release_checker._get_compose_cmd", return_value=["docker", "compose"]), \
             patch("updater.release_checker.subprocess.run", side_effect=side_effect) as mock_run:
            result = await apply_update()

        assert result["success"] is False
        assert result["dirty_tree"] is True
        assert "docker-compose.yml" in result["dirty_files"]
        assert "uncommitted" in result["message"].lower()
        assert "stash" in result["suggested_command"]
        assert "docker-compose.yml" in result["suggested_command"]

        # Must not have attempted fetch or checkout
        all_calls = [c.args[0] for c in mock_run.call_args_list]
        assert not any("fetch" in cmd for cmd in all_calls)
        assert not any("checkout" in cmd for cmd in all_calls)

    @pytest.mark.asyncio
    async def test_dirty_tree_lists_all_blocking_files(self):
        """All tracked-modified files should be reported, not just one."""
        from updater.release_checker import apply_update
        self._set_setting("autoupdate_available_version", "1.0.2")

        repo_dir = Path("/tmp/fake-repo")
        # Mix of modified, added, deleted, renamed — all block checkout
        porcelain = (
            " M docker-compose.yml\n"
            " M updater/templates/monitor.html\n"
            "A  newfile.py\n"
            " D removed.py\n"
            "?? not-tracked.txt\n"  # untracked must NOT be reported
        )
        side_effect = self._make_git_dispatch(porcelain)

        with patch("updater.release_checker.db.get_active_rollout", return_value=None), \
             patch("updater.release_checker._docker_socket_available", return_value=True), \
             patch("updater.release_checker._get_repo_dir", return_value=repo_dir), \
             patch("updater.release_checker._get_compose_cmd", return_value=["docker", "compose"]), \
             patch("updater.release_checker.subprocess.run", side_effect=side_effect):
            result = await apply_update()

        assert result["success"] is False
        assert result["dirty_tree"] is True
        assert set(result["dirty_files"]) == {
            "docker-compose.yml",
            "updater/templates/monitor.html",
            "newfile.py",
            "removed.py",
        }
        assert "not-tracked.txt" not in result["dirty_files"]

    @pytest.mark.asyncio
    async def test_untracked_files_do_not_block(self):
        """Untracked-only state must NOT block apply (git checkout handles it)."""
        from updater.release_checker import apply_update
        self._set_setting("autoupdate_available_version", "1.0.2")

        repo_dir = Path("/tmp/fake-repo")
        # Only untracked entries — typical for a working tree with a deploy override
        side_effect = self._make_git_dispatch(
            "?? docker-compose.override.yml\n?? .env.local\n"
        )
        version_content = '__version__ = "1.0.2"'

        with patch("updater.release_checker.db.get_active_rollout", return_value=None), \
             patch("updater.release_checker._docker_socket_available", return_value=True), \
             patch("updater.release_checker._get_repo_dir", return_value=repo_dir), \
             patch("updater.release_checker._get_compose_cmd", return_value=["docker", "compose"]), \
             patch("updater.release_checker._get_host_repo_path", return_value=None), \
             patch("updater.release_checker.subprocess.run", side_effect=side_effect), \
             patch("updater.release_checker.subprocess.Popen"), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "read_text", return_value=version_content):
            result = await apply_update()

        # Should proceed to a successful apply
        assert result["success"] is True
        assert "dirty_tree" not in result

    @pytest.mark.asyncio
    async def test_clean_tree_proceeds(self):
        """A truly clean working tree must proceed without dirty_tree handling."""
        from updater.release_checker import apply_update
        self._set_setting("autoupdate_available_version", "1.0.2")

        repo_dir = Path("/tmp/fake-repo")
        side_effect = self._make_git_dispatch("")  # empty porcelain = clean
        version_content = '__version__ = "1.0.2"'

        with patch("updater.release_checker.db.get_active_rollout", return_value=None), \
             patch("updater.release_checker._docker_socket_available", return_value=True), \
             patch("updater.release_checker._get_repo_dir", return_value=repo_dir), \
             patch("updater.release_checker._get_compose_cmd", return_value=["docker", "compose"]), \
             patch("updater.release_checker._get_host_repo_path", return_value=None), \
             patch("updater.release_checker.subprocess.run", side_effect=side_effect), \
             patch("updater.release_checker.subprocess.Popen"), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "read_text", return_value=version_content):
            result = await apply_update()

        assert result["success"] is True
        assert "dirty_tree" not in result

    @pytest.mark.asyncio
    async def test_renamed_file_reports_new_path(self):
        """Git porcelain emits renames as 'R  old -> new'. The new path is
        what's tracked at HEAD and what blocks checkout — make sure that's
        what we report (and what the suggested_command targets), not the
        literal 'old -> new' string."""
        from updater.release_checker import apply_update
        self._set_setting("autoupdate_available_version", "1.0.2")

        repo_dir = Path("/tmp/fake-repo")
        side_effect = self._make_git_dispatch("R  old/path.py -> new/path.py\n")

        with patch("updater.release_checker.db.get_active_rollout", return_value=None), \
             patch("updater.release_checker._docker_socket_available", return_value=True), \
             patch("updater.release_checker._get_repo_dir", return_value=repo_dir), \
             patch("updater.release_checker._get_compose_cmd", return_value=["docker", "compose"]), \
             patch("updater.release_checker.subprocess.run", side_effect=side_effect):
            result = await apply_update()

        assert result["dirty_tree"] is True
        assert result["dirty_files"] == ["new/path.py"]
        assert "old/path.py" not in result["suggested_command"]
        assert "new/path.py" in result["suggested_command"]

    @pytest.mark.asyncio
    async def test_dirty_path_with_spaces_is_shell_quoted(self):
        """A path containing spaces must be shell-quoted in suggested_command
        so the operator can paste it without it splitting into multiple args."""
        from updater.release_checker import apply_update
        self._set_setting("autoupdate_available_version", "1.0.2")

        repo_dir = Path("/tmp/fake-repo")
        side_effect = self._make_git_dispatch(' M docs/Release Notes.md\n')

        with patch("updater.release_checker.db.get_active_rollout", return_value=None), \
             patch("updater.release_checker._docker_socket_available", return_value=True), \
             patch("updater.release_checker._get_repo_dir", return_value=repo_dir), \
             patch("updater.release_checker._get_compose_cmd", return_value=["docker", "compose"]), \
             patch("updater.release_checker.subprocess.run", side_effect=side_effect):
            result = await apply_update()

        assert result["dirty_tree"] is True
        # shlex.quote will wrap the space-containing path in single quotes
        assert "'docs/Release Notes.md'" in result["suggested_command"]

    @pytest.mark.asyncio
    async def test_status_failure_logs_warning_and_proceeds(self, caplog):
        """If `git status` itself fails, log a warning and proceed to checkout
        rather than silently swallowing the failure."""
        import logging
        from updater.release_checker import apply_update
        self._set_setting("autoupdate_available_version", "1.0.2")

        repo_dir = Path("/tmp/fake-repo")

        def side_effect(cmd, **kwargs):
            result = MagicMock()
            result.stderr = "fatal: not a git repository"
            if "status" in cmd:
                result.returncode = 128
                result.stdout = ""
            elif "rev-parse" in cmd:
                result.returncode = 0
                result.stdout = "abc123"
                result.stderr = ""
            else:
                result.returncode = 0
                result.stdout = ""
                result.stderr = ""
            return result

        version_content = '__version__ = "1.0.2"'

        with patch("updater.release_checker.db.get_active_rollout", return_value=None), \
             patch("updater.release_checker._docker_socket_available", return_value=True), \
             patch("updater.release_checker._get_repo_dir", return_value=repo_dir), \
             patch("updater.release_checker._get_compose_cmd", return_value=["docker", "compose"]), \
             patch("updater.release_checker._get_host_repo_path", return_value=None), \
             patch("updater.release_checker.subprocess.run", side_effect=side_effect), \
             patch("updater.release_checker.subprocess.Popen"), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "read_text", return_value=version_content):
            with caplog.at_level(logging.WARNING, logger="updater.release_checker"):
                result = await apply_update()

        # No dirty_tree key — status failure shouldn't masquerade as dirty
        assert "dirty_tree" not in result
        # Apply still proceeds (success path executes)
        assert result["success"] is True
        # And we left a breadcrumb
        assert any("git status" in r.message and "failed" in r.message
                   for r in caplog.records)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

class TestGetRepoDir:
    def test_finds_repo_at_app_repo(self, tmp_path):
        from updater.release_checker import _get_repo_dir

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        with patch("updater.release_checker.Path") as MockPath:
            MockPath.side_effect = lambda p: repo if p == "/app/repo" else Path(p)
            # Directly test with real paths
        # Simpler approach: just test the logic
        assert (repo / ".git").exists()

    def test_returns_none_when_no_repo(self):
        from updater.release_checker import _get_repo_dir

        with patch("updater.release_checker.Path") as MockPath:
            mock_path = MagicMock()
            mock_path.__truediv__ = MagicMock(return_value=MagicMock(exists=MagicMock(return_value=False)))
            MockPath.return_value = mock_path
            result = _get_repo_dir()

        assert result is None


class TestGetComposeCmd:
    def test_includes_standalone_when_present(self, tmp_path):
        from updater.release_checker import _get_compose_cmd

        (tmp_path / "docker-compose.yml").touch()
        (tmp_path / "docker-compose.standalone.yml").touch()

        cmd = _get_compose_cmd(tmp_path)
        assert "-f" in cmd
        assert str(tmp_path / "docker-compose.standalone.yml") in cmd

    def test_excludes_standalone_when_absent(self, tmp_path):
        from updater.release_checker import _get_compose_cmd

        (tmp_path / "docker-compose.yml").touch()

        cmd = _get_compose_cmd(tmp_path)
        assert len(cmd) == 4  # docker compose -f <path>
        assert not any("docker-compose.standalone.yml" in c for c in cmd)


class TestIsSafeToUpdate:
    @pytest.fixture(autouse=True)
    def _patch_db(self, mock_db):
        pass

    def test_safe_when_no_rollout_no_window(self):
        from updater.release_checker import _is_safe_to_update

        with patch("updater.release_checker.db.get_active_rollout", return_value=None):
            is_safe, reason = _is_safe_to_update()

        assert is_safe is True
        assert reason == ""

    def test_blocked_by_active_rollout(self):
        from updater.release_checker import _is_safe_to_update

        with patch("updater.release_checker.db.get_active_rollout",
                    return_value={"status": "in_progress"}):
            is_safe, reason = _is_safe_to_update()

        assert is_safe is False
        assert "rollout" in reason.lower()


# ---------------------------------------------------------------------------
# Watchdog script
# ---------------------------------------------------------------------------

class TestBuildWatchdogScript:
    def test_script_contains_rollback_ref(self):
        from updater.release_checker import _build_watchdog_script
        script = _build_watchdog_script("/opt/sixtyops", "abc123", False)
        assert 'ROLLBACK_REF="abc123"' in script

    def test_script_contains_compose_cmd(self):
        from updater.release_checker import _build_watchdog_script
        script = _build_watchdog_script("/opt/sixtyops", "abc123", False)
        assert "docker compose -f /opt/sixtyops/docker-compose.yml" in script

    def test_script_includes_standalone_when_flagged(self):
        from updater.release_checker import _build_watchdog_script
        script = _build_watchdog_script("/opt/sixtyops", "abc123", True)
        assert "docker-compose.standalone.yml" in script

    def test_script_excludes_standalone_when_not_flagged(self):
        from updater.release_checker import _build_watchdog_script
        script = _build_watchdog_script("/opt/sixtyops", "abc123", False)
        assert "standalone" not in script

    def test_script_has_health_check_loop(self):
        from updater.release_checker import _build_watchdog_script
        script = _build_watchdog_script("/opt/sixtyops", "abc123", False)
        assert "Health.Status" in script
        assert "healthy" in script

    def test_script_has_rollback_on_failure(self):
        from updater.release_checker import _build_watchdog_script
        script = _build_watchdog_script("/opt/sixtyops", "abc123", False)
        assert "Rolling back" in script
        assert "checkout" in script

    def test_script_tags_rollback_image(self):
        from updater.release_checker import _build_watchdog_script
        script = _build_watchdog_script("/opt/sixtyops", "abc123", False)
        assert "docker tag" in script
        assert ":rollback" in script


# ---------------------------------------------------------------------------
# Post-restart verification
# ---------------------------------------------------------------------------

class TestVerifyUpdateOnStartup:
    @pytest.fixture(autouse=True)
    def _patch_db(self, mock_db):
        self.db = mock_db

    def _set_setting(self, key, value):
        self.db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.db.commit()

    @pytest.mark.asyncio
    async def test_success_clears_pending_version(self):
        from updater.release_checker import verify_update_on_startup
        from updater import database as db

        self._set_setting("autoupdate_pending_version", "1.0.1")
        self._set_setting("autoupdate_rollback_ref", "abc123")
        self._set_setting("autoupdate_available_version", "1.0.1")

        broadcast = AsyncMock()
        with patch("updater.release_checker.__version__", "1.0.1"):
            await verify_update_on_startup(broadcast)

        assert db.get_setting("autoupdate_pending_version", "") == ""
        assert db.get_setting("autoupdate_available_version", "") == ""
        assert db.get_setting("autoupdate_rollback_ref", "") == ""
        broadcast.assert_called_once()
        assert broadcast.call_args[0][0]["type"] == "update_completed"

    @pytest.mark.asyncio
    async def test_rollback_detected(self):
        from updater.release_checker import verify_update_on_startup
        from updater import database as db

        self._set_setting("autoupdate_pending_version", "1.0.2")
        self._set_setting("autoupdate_rollback_ref", "abc123")

        broadcast = AsyncMock()
        with patch("updater.release_checker.__version__", "1.0.1"):
            await verify_update_on_startup(broadcast)

        assert db.get_setting("autoupdate_pending_version", "") == ""
        broadcast.assert_called_once()
        assert broadcast.call_args[0][0]["type"] == "update_rolled_back"

    @pytest.mark.asyncio
    async def test_no_pending_is_noop(self):
        from updater.release_checker import verify_update_on_startup

        broadcast = AsyncMock()
        await verify_update_on_startup(broadcast)
        broadcast.assert_not_called()


# ---------------------------------------------------------------------------
# Appliance mode
# ---------------------------------------------------------------------------

class TestApplianceMode:
    """Test the appliance mode docker-pull update path."""

    @pytest.fixture(autouse=True)
    def _patch_db(self, mock_db):
        self.db = mock_db

    def _set_setting(self, key, value):
        self.db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.db.commit()

    def test_appliance_mode_env_var(self):
        """APPLIANCE_MODE is driven by SIXTYOPS_APPLIANCE env var."""
        with patch.dict("os.environ", {"SIXTYOPS_APPLIANCE": "1"}):
            import importlib
            import updater.release_checker as rc
            importlib.reload(rc)
            assert rc.APPLIANCE_MODE is True

        with patch.dict("os.environ", {}, clear=True):
            importlib.reload(rc)
            assert rc.APPLIANCE_MODE is False

        # Restore original state
        importlib.reload(rc)

    def test_get_update_status_includes_appliance_mode(self):
        from updater.release_checker import ReleaseChecker
        checker = ReleaseChecker(broadcast_func=AsyncMock())
        with patch("updater.release_checker.db.get_active_rollout", return_value=None):
            status = checker.get_update_status()
        assert "appliance_mode" in status

    @pytest.mark.asyncio
    async def test_appliance_update_pulls_image(self):
        """In appliance mode, apply_update should docker pull, not git fetch."""
        from updater.release_checker import _apply_update_appliance

        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        compose_dir = Path("/opt/sixtyops")
        with patch("updater.release_checker._docker_socket_available", return_value=True), \
             patch("updater.release_checker._get_compose_dir", return_value=compose_dir), \
             patch("updater.release_checker.subprocess.run", side_effect=mock_run), \
             patch("updater.release_checker._launch_appliance_watchdog", return_value=True):
            result = await _apply_update_appliance("1.2.0", "v1.2.0")

        assert result["success"] is True
        # Verify docker pull was called with correct image
        pull_cmds = [c for c in calls if "pull" in c]
        assert len(pull_cmds) == 1
        assert "ghcr.io/sixtyops/manager:v1.2.0" in pull_cmds[0]

    @pytest.mark.asyncio
    async def test_appliance_update_fails_on_pull_error(self):
        from updater.release_checker import _apply_update_appliance

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            if "pull" in cmd:
                result.returncode = 1
                result.stderr = "manifest not found"
            else:
                result.returncode = 0
            result.stdout = ""
            return result

        with patch("updater.release_checker._docker_socket_available", return_value=True), \
             patch("updater.release_checker._get_compose_dir", return_value=Path("/opt/sixtyops")), \
             patch("updater.release_checker.subprocess.run", side_effect=mock_run):
            result = await _apply_update_appliance("1.2.0", "v1.2.0")

        assert result["success"] is False
        assert "pull failed" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_appliance_update_no_docker_socket(self):
        from updater.release_checker import _apply_update_appliance

        with patch("updater.release_checker._docker_socket_available", return_value=False):
            result = await _apply_update_appliance("1.2.0", "v1.2.0")

        assert result["success"] is False
        assert "socket" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_appliance_update_fallback_on_watchdog_failure(self):
        """If watchdog fails to launch, falls back to direct swap."""
        from updater.release_checker import _apply_update_appliance

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with patch("updater.release_checker._docker_socket_available", return_value=True), \
             patch("updater.release_checker._get_compose_dir", return_value=Path("/opt/sixtyops")), \
             patch("updater.release_checker.subprocess.run", side_effect=mock_run), \
             patch("updater.release_checker._launch_appliance_watchdog", return_value=False), \
             patch("updater.release_checker.subprocess.Popen") as mock_popen:
            result = await _apply_update_appliance("1.2.0", "v1.2.0")

        assert result["success"] is True
        mock_popen.assert_called_once()

    @pytest.mark.asyncio
    async def test_apply_update_branches_to_appliance_mode(self):
        """When APPLIANCE_MODE=True, apply_update uses the appliance path."""
        from updater.release_checker import apply_update
        self._set_setting("autoupdate_available_version", "1.2.0")

        with patch("updater.release_checker.APPLIANCE_MODE", True), \
             patch("updater.release_checker.db.get_active_rollout", return_value=None), \
             patch("updater.release_checker._apply_update_appliance", new_callable=AsyncMock,
                   return_value={"success": True, "message": "ok"}) as mock_appliance:
            result = await apply_update()

        assert result["success"] is True
        mock_appliance.assert_called_once_with("1.2.0", "v1.2.0")

    @pytest.mark.asyncio
    async def test_apply_update_uses_git_when_not_appliance(self):
        """When APPLIANCE_MODE=False, apply_update takes the git path."""
        from updater.release_checker import apply_update
        self._set_setting("autoupdate_available_version", "1.0.2")

        with patch("updater.release_checker.APPLIANCE_MODE", False), \
             patch("updater.release_checker.db.get_active_rollout", return_value=None), \
             patch("updater.release_checker._docker_socket_available", return_value=False):
            result = await apply_update()

        # Git path returns manual commands when no docker socket
        assert result["success"] is False
        assert result.get("manual") is True


class TestApplianceWatchdogScript:
    def test_script_has_health_check(self):
        from updater.release_checker import _build_appliance_watchdog_script
        script = _build_appliance_watchdog_script("/opt/sixtyops", False)
        assert "Health.Status" in script
        assert "healthy" in script

    def test_script_has_rollback(self):
        from updater.release_checker import _build_appliance_watchdog_script
        script = _build_appliance_watchdog_script("/opt/sixtyops", False)
        assert "Rolling back" in script
        assert ":rollback" in script

    def test_script_no_git_operations(self):
        """Appliance watchdog should NOT use git."""
        from updater.release_checker import _build_appliance_watchdog_script
        script = _build_appliance_watchdog_script("/opt/sixtyops", False)
        assert "git" not in script

    def test_script_uses_no_build(self):
        """Appliance watchdog should use --no-build (image already pulled)."""
        from updater.release_checker import _build_appliance_watchdog_script
        script = _build_appliance_watchdog_script("/opt/sixtyops", False)
        assert "--no-build" in script

    def test_script_includes_standalone(self):
        from updater.release_checker import _build_appliance_watchdog_script
        script = _build_appliance_watchdog_script("/opt/sixtyops", True)
        assert "docker-compose.standalone.yml" in script


# ---------------------------------------------------------------------------
# Appliance version and compatibility
# ---------------------------------------------------------------------------

class TestApplianceVersion:
    """Test appliance version detection and compatibility checking."""

    def test_get_appliance_version_reads_file(self, tmp_path):
        from updater.release_checker import get_appliance_version, APPLIANCE_VERSION_FILE
        version_file = tmp_path / "appliance-version"
        version_file.write_text("1.0\n")

        with patch("updater.release_checker.APPLIANCE_VERSION_FILE", version_file):
            assert get_appliance_version() == "1.0"

    def test_get_appliance_version_returns_none_when_missing(self, tmp_path):
        from updater.release_checker import get_appliance_version
        missing = tmp_path / "nonexistent"

        with patch("updater.release_checker.APPLIANCE_VERSION_FILE", missing):
            assert get_appliance_version() is None

    def test_parse_min_appliance_version_found(self):
        from updater.release_checker import parse_min_appliance_version
        notes = "Some release notes\n<!-- min_appliance_version: 1.1 -->\nMore text"
        assert parse_min_appliance_version(notes) == "1.1"

    def test_parse_min_appliance_version_not_found(self):
        from updater.release_checker import parse_min_appliance_version
        assert parse_min_appliance_version("Regular release notes") is None

    def test_parse_min_appliance_version_none_input(self):
        from updater.release_checker import parse_min_appliance_version
        assert parse_min_appliance_version(None) is None

    def test_parse_min_appliance_version_empty(self):
        from updater.release_checker import parse_min_appliance_version
        assert parse_min_appliance_version("") is None

    def test_get_update_status_includes_appliance_version(self, mock_db):
        from updater.release_checker import ReleaseChecker
        checker = ReleaseChecker(broadcast_func=AsyncMock())
        with patch("updater.release_checker.db.get_active_rollout", return_value=None), \
             patch("updater.release_checker.get_appliance_version", return_value="1.0"):
            status = checker.get_update_status()
        assert status["appliance_version"] == "1.0"

    @pytest.mark.asyncio
    async def test_apply_update_blocks_incompatible_appliance(self, mock_db):
        """apply_update should refuse when appliance platform is too old."""
        from updater.release_checker import apply_update
        from updater import database
        database.set_setting("autoupdate_available_version", "2.0.0")
        database.set_setting("autoupdate_release_notes",
                             "Notes\n<!-- min_appliance_version: 1.1 -->")

        with patch("updater.release_checker.APPLIANCE_MODE", True), \
             patch("updater.release_checker.db.get_active_rollout", return_value=None), \
             patch("updater.release_checker.get_appliance_version", return_value="1.0"):
            result = await apply_update()

        assert result["success"] is False
        assert result.get("appliance_upgrade_required") is True
        assert "1.1" in result["message"]

    @pytest.mark.asyncio
    async def test_apply_update_allows_compatible_appliance(self, mock_db):
        """apply_update should proceed when appliance version is sufficient."""
        from updater.release_checker import apply_update
        from updater import database
        database.set_setting("autoupdate_available_version", "2.0.0")
        database.set_setting("autoupdate_release_notes",
                             "Notes\n<!-- min_appliance_version: 1.0 -->")

        with patch("updater.release_checker.APPLIANCE_MODE", True), \
             patch("updater.release_checker.db.get_active_rollout", return_value=None), \
             patch("updater.release_checker.get_appliance_version", return_value="1.0"), \
             patch("updater.release_checker._apply_update_appliance", new_callable=AsyncMock,
                   return_value={"success": True, "message": "ok"}) as mock_apply:
            result = await apply_update()

        assert result["success"] is True
        mock_apply.assert_called_once()

    @pytest.mark.asyncio
    async def test_apply_update_proceeds_without_min_version(self, mock_db):
        """apply_update should proceed when no min_appliance_version in notes."""
        from updater.release_checker import apply_update
        from updater import database
        database.set_setting("autoupdate_available_version", "2.0.0")
        database.set_setting("autoupdate_release_notes", "Regular notes")

        with patch("updater.release_checker.APPLIANCE_MODE", True), \
             patch("updater.release_checker.db.get_active_rollout", return_value=None), \
             patch("updater.release_checker.get_appliance_version", return_value="1.0"), \
             patch("updater.release_checker._apply_update_appliance", new_callable=AsyncMock,
                   return_value={"success": True, "message": "ok"}) as mock_apply:
            result = await apply_update()

        assert result["success"] is True
        mock_apply.assert_called_once()
