"""Unit tests for Module 08 — CLI Interface."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import typer
from typer.testing import CliRunner

from dotsync.config import default_config
from dotsync.discovery import ConfigFile
from dotsync.flagging import FlagResult, SensitiveMatch
from dotsync.main import EXIT_CODES, app, confirm_sensitive_files

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

        with (
            patch("dotsync.discovery.discover", return_value=[]),
            patch("dotsync.flagging.flag_all", return_value=[]),
            patch("dotsync.flagging.enforce_never_include", return_value=[]),
            patch("dotsync.git_ops.init_repo") as mock_repo,
            patch("dotsync.git_ops.load_manifest", return_value=[]),
            patch("dotsync.sync.register_new_files", return_value=[]),
            patch("dotsync.sync.plan_sync", return_value=[]),
            patch("dotsync.sync.execute_sync", return_value=[]),
            patch("dotsync.git_ops.commit_and_push"),
            patch("dotsync.health.post_operation_checks", side_effect=HealthCheckFailedError("git check failed")),
            patch("dotsync.snapshot.create_snapshot") as mock_snap,
            patch("dotsync.platform_utils.home_dir", return_value=Path("/home/test")),
            patch("dotsync.platform_utils.current_os", return_value="linux"),
        ):
            mock_snap.return_value = MagicMock(id="2026-01-01T00-00-00", file_count=3)
            mock_repo.return_value = MagicMock()
            # Need manifest to be non-empty to trigger snapshot + health checks
            with patch("dotsync.git_ops.load_manifest") as mock_manifest:
                mock_manifest.side_effect = [
                    [MagicMock(relative_path=".bashrc", os_profile="shared")],
                    [MagicMock(relative_path=".bashrc", os_profile="shared")],
                ]
                result = runner.invoke(app, ["sync"])

        assert result.exit_code == EXIT_CODES["health_check_failed"]

    @patch("dotsync.config.CONFIG_FILE", new_callable=lambda: MagicMock(exists=MagicMock(return_value=False)))
    @patch("dotsync.git_ops.check_dependencies", side_effect=__import__("dotsync.git_ops", fromlist=["MissingDependencyError"]).MissingDependencyError("git not found"))
    def test_exit_code_on_missing_dependency(self, mock_check: MagicMock, mock_config: MagicMock) -> None:
        """Init should exit with code 2 when dependencies are missing."""
        result = runner.invoke(app, ["init"])

        assert result.exit_code == EXIT_CODES["dependency_missing"]


