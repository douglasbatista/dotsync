"""Unit tests for Module 08 — CLI Interface."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import typer
from typer.testing import CliRunner

from dotsync.config import default_config
from dotsync.discovery import ConfigFile
from dotsync.flagging import FlagResult, SensitiveMatch
from dotsync.git_ops import ManifestEntry
from dotsync.main import EXIT_CODES, _mark_sensitive, app, confirm_sensitive_files

runner = CliRunner()


# ---------------------------------------------------------------------------
# TestConfirmSensitiveFiles
# ---------------------------------------------------------------------------


class TestConfirmSensitiveFiles:
    """Tests for the confirm_sensitive_files() UI function."""

    def _make_flag_result(self, path: str = ".bashrc") -> FlagResult:
        """Create a FlagResult that requires confirmation."""
        cf = ConfigFile(
            path=Path(path),
            abs_path=Path.home() / path,
            size_bytes=100,
            include=True,
            reason="home dotfile",
            os_profile="shared",
        )
        return FlagResult(
            config_file=cf,
            matches=[SensitiveMatch(pattern_name="generic_token", line_number=5, preview="to***en")],
            ai_flagged=False,
            requires_confirmation=True,
        )

    @patch("dotsync.main.typer.prompt", return_value="I")
    def test_confirm_sensitive_include_updates_flag_result(self, mock_prompt: MagicMock) -> None:
        """User choosing Include should clear requires_confirmation."""
        fr = self._make_flag_result()
        results = [fr]

        confirm_sensitive_files(results)

        assert fr.requires_confirmation is False
        assert fr.config_file.include is True

    @patch("dotsync.main.typer.prompt", return_value="E")
    def test_confirm_sensitive_exclude_updates_flag_result(self, mock_prompt: MagicMock) -> None:
        """User choosing Exclude should set include=False and clear confirmation."""
        fr = self._make_flag_result()
        results = [fr]

        confirm_sensitive_files(results)

        assert fr.requires_confirmation is False
        assert fr.config_file.include is False

    @patch("dotsync.main.typer.prompt", return_value="S")
    def test_confirm_sensitive_skip_leaves_ask_user(self, mock_prompt: MagicMock) -> None:
        """User choosing Skip should leave requires_confirmation as True."""
        fr = self._make_flag_result()
        results = [fr]

        confirm_sensitive_files(results)

        assert fr.requires_confirmation is True
        assert fr.config_file.include is True


# ---------------------------------------------------------------------------
# TestMarkSensitive
# ---------------------------------------------------------------------------


class TestMarkSensitive:
    """Tests for _mark_sensitive() helper."""

    def test_mark_sensitive_sets_flag_on_included_match(self) -> None:
        """File with matches and confirmed (requires_confirmation=False) should be marked sensitive."""
        cf = ConfigFile(
            path=Path(".env"),
            abs_path=Path.home() / ".env",
            size_bytes=50,
            include=True,
            reason="home dotfile",
            os_profile="shared",
        )
        fr = FlagResult(
            config_file=cf,
            matches=[SensitiveMatch(pattern_name="generic_token", line_number=1, preview="to***en")],
            ai_flagged=False,
            requires_confirmation=False,
        )
        _mark_sensitive([fr])
        assert cf.sensitive is True

    def test_mark_sensitive_sets_flag_on_ai_flagged(self) -> None:
        """AI-flagged file that was confirmed should be marked sensitive."""
        cf = ConfigFile(
            path=Path(".secrets"),
            abs_path=Path.home() / ".secrets",
            size_bytes=50,
            include=True,
            reason="home dotfile",
            os_profile="shared",
        )
        fr = FlagResult(
            config_file=cf,
            matches=[],
            ai_flagged=True,
            requires_confirmation=False,
        )
        _mark_sensitive([fr])
        assert cf.sensitive is True

    def test_mark_sensitive_skips_unconfirmed(self) -> None:
        """File still requiring confirmation should not be marked sensitive."""
        cf = ConfigFile(
            path=Path(".bashrc"),
            abs_path=Path.home() / ".bashrc",
            size_bytes=100,
            include=True,
            reason="home dotfile",
            os_profile="shared",
        )
        fr = FlagResult(
            config_file=cf,
            matches=[SensitiveMatch(pattern_name="generic_token", line_number=1, preview="to***en")],
            ai_flagged=False,
            requires_confirmation=True,
        )
        _mark_sensitive([fr])
        assert cf.sensitive is False

    def test_mark_sensitive_skips_clean_files(self) -> None:
        """File with no matches and not AI-flagged should stay sensitive=False."""
        cf = ConfigFile(
            path=Path(".bashrc"),
            abs_path=Path.home() / ".bashrc",
            size_bytes=100,
            include=True,
            reason="home dotfile",
            os_profile="shared",
        )
        fr = FlagResult(
            config_file=cf,
            matches=[],
            ai_flagged=False,
            requires_confirmation=False,
        )
        _mark_sensitive([fr])
        assert cf.sensitive is False


# ---------------------------------------------------------------------------
# TestConfigCommand
# ---------------------------------------------------------------------------


class TestConfigCommand:
    """Tests for the config command."""

    @patch("dotsync.main.load_config" if False else "dotsync.config.save_config")
    @patch("dotsync.config.load_config")
    def test_config_set_updates_value(self, mock_load: MagicMock, mock_save: MagicMock) -> None:
        """--set KEY=VALUE should update the config field and save."""
        cfg = default_config()
        mock_load.return_value = cfg

        with patch("dotsync.main.typer.Exit", side_effect=typer.Exit):
            runner.invoke(app, ["config", "--set", "snapshot_keep=10"])

        # The config object should have been updated
        assert cfg.snapshot_keep == 10
        mock_save.assert_called_once()

    @patch("dotsync.config.load_config")
    def test_config_set_rejects_unknown_key(self, mock_load: MagicMock) -> None:
        """--set with an unknown key should exit with error."""
        mock_load.return_value = default_config()

        result = runner.invoke(app, ["config", "--set", "nonexistent_key=value"])

        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# TestErrorHandling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Tests for CLI exit codes on error conditions."""

    @patch("dotsync.config.load_config")
    def test_exit_code_on_health_check_failure(self, mock_load: MagicMock) -> None:
        """Sync should exit with code 1 when health checks fail."""
        from dotsync.health import HealthCheckFailedError

        cfg = default_config()
        mock_load.return_value = cfg

        manifest = [
            ManifestEntry(
                relative_path=".bashrc",
                os_profile="shared",
                added_at="2026-01-01",
                sensitive_flagged=False,
            ),
        ]

        with (
            patch("dotsync.git_ops.load_manifest", return_value=manifest),
            patch("dotsync.flagging.flag_all", return_value=[]),
            patch("dotsync.git_ops.init_repo") as mock_repo,
            patch("dotsync.snapshot.create_snapshot") as mock_snap,
            patch("dotsync.sync.plan_sync") as mock_plan,
            patch("dotsync.sync.execute_sync", return_value=[]),
            patch("dotsync.git_ops.commit_and_push"),
            patch("dotsync.health.post_operation_checks", side_effect=HealthCheckFailedError("git check failed")),
            patch("dotsync.platform_utils.home_dir", return_value=Path("/home/test")),
            patch("dotsync.platform_utils.current_os", return_value="linux"),
            patch("dotsync.main.typer.confirm", return_value=True),
        ):
            mock_snap.return_value = MagicMock(id="2026-01-01T00-00-00", file_count=3)
            mock_repo.return_value = MagicMock()
            # plan_sync returns one copy action so sync proceeds
            mock_action = MagicMock(action="copy", source=Path("/home/test/.bashrc"))
            mock_plan.return_value = [mock_action]

            result = runner.invoke(app, ["sync"])

        assert result.exit_code == EXIT_CODES["health_check_failed"]

    @patch("dotsync.config.CONFIG_FILE", new_callable=lambda: MagicMock(exists=MagicMock(return_value=False)))
    @patch("dotsync.git_ops.check_dependencies", side_effect=__import__("dotsync.git_ops", fromlist=["MissingDependencyError"]).MissingDependencyError("git not found"))
    def test_exit_code_on_missing_dependency(self, mock_check: MagicMock, mock_config: MagicMock) -> None:
        """Init should exit with code 2 when dependencies are missing."""
        result = runner.invoke(app, ["init"])

        assert result.exit_code == EXIT_CODES["dependency_missing"]


# ---------------------------------------------------------------------------
# TestDiscoverProgress
# ---------------------------------------------------------------------------


class TestDiscoverProgress:
    """Tests for scan progress display wiring."""

    @patch("dotsync.config.load_config")
    def test_discover_passes_progress_callback(self, mock_load: MagicMock) -> None:
        """discover command should call discover() with a callable progress kwarg."""
        cfg = default_config()
        mock_load.return_value = cfg

        with (
            patch("dotsync.discovery.discover") as mock_discover,
            patch("dotsync.flagging.enforce_never_include", return_value=[]),
            patch("dotsync.flagging.flag_all", return_value=[]),
            patch("dotsync.git_ops.load_manifest", return_value=[]),
        ):
            mock_discover.return_value = []
            result = runner.invoke(app, ["discover", "--no-ai"])

        assert result.exit_code == 0
        mock_discover.assert_called_once()
        _, kwargs = mock_discover.call_args
        assert "progress" in kwargs
        assert callable(kwargs["progress"])

    @patch("dotsync.config.load_config")
    def test_discover_verbose_logs_pruned_dirs(self, mock_load: MagicMock) -> None:
        """With --verbose, dir_pruned and file_rejected events should be logged at DEBUG."""
        from dotsync.discovery import ScanEvent

        cfg = default_config()
        mock_load.return_value = cfg

        captured_callback: list = []

        def fake_discover(cfg: object, progress: object = None) -> list:
            captured_callback.append(progress)
            # Simulate events
            if callable(progress):
                progress(ScanEvent(
                    type="dir_pruned",
                    path="/home/test/.cache",
                    reason="dir_name in PRUNE_DIRS",
                    count=None,
                ))
                progress(ScanEvent(
                    type="file_rejected",
                    path="/home/test/photo.png",
                    reason="blocked extension: .png",
                    count=None,
                ))
            return []

        with (
            patch("dotsync.discovery.discover", side_effect=fake_discover),
            patch("dotsync.main.logger") as mock_logger,
            patch("dotsync.flagging.enforce_never_include", return_value=[]),
            patch("dotsync.flagging.flag_all", return_value=[]),
            patch("dotsync.git_ops.load_manifest", return_value=[]),
        ):
            result = runner.invoke(app, ["--verbose", "discover", "--no-ai"])

        assert result.exit_code == 0
        # Verify debug logs were emitted for pruned/rejected events
        debug_calls = mock_logger.debug.call_args_list
        assert len(debug_calls) >= 2
        # First call should be dir_pruned
        assert "dir_pruned" in str(debug_calls[0])
        # Second call should be file_rejected
        assert "file_rejected" in str(debug_calls[1])

    @patch("dotsync.config.load_config")
    def test_discover_verbose_logs_accepted_files_after_scan(self, mock_load: MagicMock) -> None:
        """After scan phase, --verbose should log all accepted file paths."""
        from dotsync.discovery import ScanEvent

        cfg = default_config()
        mock_load.return_value = cfg

        def fake_discover(cfg: object, progress: object = None) -> list:
            if callable(progress):
                progress(ScanEvent(
                    type="phase_start",
                    path=None,
                    reason="scan",
                    count=None,
                ))
                progress(ScanEvent(
                    type="file_accepted",
                    path="/home/test/.bashrc",
                    reason=None,
                    count=None,
                ))
                progress(ScanEvent(
                    type="file_accepted",
                    path="/home/test/.config/nvim/init.lua",
                    reason=None,
                    count=None,
                ))
                progress(ScanEvent(
                    type="phase_done",
                    path=None,
                    reason="scan",
                    count=2,
                ))
            return []

        with (
            patch("dotsync.discovery.discover", side_effect=fake_discover),
            patch("dotsync.main.logger") as mock_logger,
            patch("dotsync.flagging.enforce_never_include", return_value=[]),
            patch("dotsync.flagging.flag_all", return_value=[]),
            patch("dotsync.git_ops.load_manifest", return_value=[]),
        ):
            result = runner.invoke(app, ["--verbose", "discover", "--no-ai"])

        assert result.exit_code == 0
        debug_calls = mock_logger.debug.call_args_list
        accepted_logs = [c for c in debug_calls if "accepted:" in str(c)]
        assert len(accepted_logs) == 2
        assert "/home/test/.bashrc" in str(accepted_logs[0])
        assert "/home/test/.config/nvim/init.lua" in str(accepted_logs[1])


# ---------------------------------------------------------------------------
# TestDiscoverRegistration
# ---------------------------------------------------------------------------


class TestDiscoverRegistration:
    """Tests for the discover command's registration flow."""

    @patch("dotsync.config.load_config")
    def test_discover_registers_new_files(self, mock_load: MagicMock) -> None:
        """discover should call register_new_files for included, untracked files."""
        cfg = default_config()
        mock_load.return_value = cfg

        files = [
            ConfigFile(
                path=Path(".bashrc"),
                abs_path=Path("/home/test/.bashrc"),
                size_bytes=100,
                include=True,
                reason="known",
                os_profile="shared",
            ),
        ]

        with (
            patch("dotsync.discovery.discover", return_value=files),
            patch("dotsync.flagging.enforce_never_include"),
            patch("dotsync.flagging.flag_all", return_value=[]),
            patch("dotsync.git_ops.load_manifest", return_value=[]),
            patch("dotsync.git_ops.init_repo"),
            patch("dotsync.platform_utils.home_dir", return_value=Path("/home/test")),
            patch("dotsync.sync.register_new_files", return_value=[MagicMock()]) as mock_register,
            patch("dotsync.main.typer.confirm", return_value=True),
        ):
            result = runner.invoke(app, ["discover", "--no-ai"])

        assert result.exit_code == 0
        mock_register.assert_called_once()

    @patch("dotsync.config.load_config")
    def test_discover_skips_already_tracked(self, mock_load: MagicMock) -> None:
        """discover should not try to register files already in manifest."""
        cfg = default_config()
        mock_load.return_value = cfg

        files = [
            ConfigFile(
                path=Path(".bashrc"),
                abs_path=Path("/home/test/.bashrc"),
                size_bytes=100,
                include=True,
                reason="known",
                os_profile="shared",
            ),
        ]

        manifest = [
            ManifestEntry(
                relative_path=".bashrc",
                os_profile="shared",
                added_at="2026-01-01",
                sensitive_flagged=False,
            ),
        ]

        with (
            patch("dotsync.discovery.discover", return_value=files),
            patch("dotsync.flagging.enforce_never_include"),
            patch("dotsync.flagging.flag_all", return_value=[]),
            patch("dotsync.git_ops.load_manifest", return_value=manifest),
        ):
            result = runner.invoke(app, ["discover", "--no-ai"])

        assert result.exit_code == 0
        # Should print "Nothing new to register"
        assert "Nothing new to register" in result.output


# ---------------------------------------------------------------------------
# TestSyncManifestBased
# ---------------------------------------------------------------------------


class TestSyncManifestBased:
    """Tests for the sync command's manifest-based flow."""

    @patch("dotsync.config.load_config")
    def test_sync_exits_when_manifest_empty(self, mock_load: MagicMock) -> None:
        """sync should exit with a message when manifest is empty."""
        cfg = default_config()
        mock_load.return_value = cfg

        with (
            patch("dotsync.git_ops.load_manifest", return_value=[]),
            patch("dotsync.platform_utils.home_dir", return_value=Path("/home/test")),
            patch("dotsync.platform_utils.current_os", return_value="linux"),
        ):
            result = runner.invoke(app, ["sync"])

        assert result.exit_code == EXIT_CODES["user_aborted"]
        assert "dotsync discover" in result.output

    @patch("dotsync.config.load_config")
    def test_sync_does_not_run_discovery(self, mock_load: MagicMock) -> None:
        """sync should NOT call discover() — it works from manifest only."""
        cfg = default_config()
        mock_load.return_value = cfg

        manifest = [
            ManifestEntry(
                relative_path=".bashrc",
                os_profile="shared",
                added_at="2026-01-01",
                sensitive_flagged=False,
            ),
        ]

        with (
            patch("dotsync.git_ops.load_manifest", return_value=manifest),
            patch("dotsync.flagging.flag_all", return_value=[]),
            patch("dotsync.git_ops.init_repo") as mock_repo,
            patch("dotsync.snapshot.create_snapshot") as mock_snap,
            patch("dotsync.sync.plan_sync", return_value=[]),
            patch("dotsync.platform_utils.home_dir", return_value=Path("/home/test")),
            patch("dotsync.platform_utils.current_os", return_value="linux"),
            patch("dotsync.discovery.discover") as mock_discover,
        ):
            mock_snap.return_value = MagicMock(id="snap-1", file_count=1)
            mock_repo.return_value = MagicMock()

            result = runner.invoke(app, ["sync", "--dry-run"])

        assert result.exit_code == 0
        mock_discover.assert_not_called()

    @patch("dotsync.config.load_config")
    def test_sync_dry_run_exits_early(self, mock_load: MagicMock) -> None:
        """sync --dry-run should display plan and exit without executing."""
        cfg = default_config()
        mock_load.return_value = cfg

        manifest = [
            ManifestEntry(
                relative_path=".bashrc",
                os_profile="shared",
                added_at="2026-01-01",
                sensitive_flagged=False,
            ),
        ]

        mock_action = MagicMock(action="copy", source=Path("/home/test/.bashrc"), transformed=False)

        with (
            patch("dotsync.git_ops.load_manifest", return_value=manifest),
            patch("dotsync.flagging.flag_all", return_value=[]),
            patch("dotsync.git_ops.init_repo") as mock_repo,
            patch("dotsync.snapshot.create_snapshot") as mock_snap,
            patch("dotsync.sync.plan_sync", return_value=[mock_action]),
            patch("dotsync.sync.execute_sync") as mock_execute,
            patch("dotsync.platform_utils.home_dir", return_value=Path("/home/test")),
            patch("dotsync.platform_utils.current_os", return_value="linux"),
        ):
            mock_snap.return_value = MagicMock(id="snap-1", file_count=1)
            mock_repo.return_value = MagicMock()

            result = runner.invoke(app, ["sync", "--dry-run"])

        assert result.exit_code == 0
        mock_execute.assert_not_called()
        assert "Dry run" in result.output
