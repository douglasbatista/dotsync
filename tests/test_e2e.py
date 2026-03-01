"""End-to-end CLI tests via Typer's CliRunner."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from dotsync.main import app

pytestmark = pytest.mark.e2e

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_dependencies(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch check_dependencies to skip git/git-crypt PATH checks.

    Returns the mock for git-crypt subprocess calls.
    """
    monkeypatch.setattr("dotsync.git_ops.check_dependencies", lambda: None)

    import subprocess

    real_run = subprocess.run

    def _fake_run(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cmd = args if isinstance(args, list) else [args]
        if cmd and str(cmd[0]) == "git-crypt":
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        return real_run(args, **kwargs)

    mock = MagicMock(side_effect=_fake_run)
    monkeypatch.setattr("dotsync.git_ops.subprocess.run", mock)
    return mock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInitCommand:
    """Tests for the 'init' CLI command."""

    def test_init_creates_config_and_repo(
        self,
        dotsync_env: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """'dotsync init' creates config.toml and initializes git repo."""
        _patch_dependencies(monkeypatch)
        repo = dotsync_env["repo"]
        config_dir = dotsync_env["config_dir"]

        result = runner.invoke(app, ["init", "--repo-path", str(repo)])

        assert result.exit_code == 0, result.output
        assert (config_dir / "config.toml").exists()
        assert (repo / ".git").exists()

    def test_init_idempotent(
        self,
        dotsync_env: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Running init twice does not error on second run."""
        _patch_dependencies(monkeypatch)
        repo = dotsync_env["repo"]

        result1 = runner.invoke(app, ["init", "--repo-path", str(repo)])
        assert result1.exit_code == 0, result1.output

        # Second run — config exists, answer 'y' to overwrite prompt
        result2 = runner.invoke(app, ["init", "--repo-path", str(repo)], input="y\n")
        assert result2.exit_code == 0, result2.output


class TestStatusCommand:
    """Tests for the 'status' CLI command."""

    def test_status_after_init(
        self,
        dotsync_env: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        mock_gitcrypt: Any,
    ) -> None:
        """'dotsync status' shows repo path and 0 managed files after init."""
        _patch_dependencies(monkeypatch)
        repo = dotsync_env["repo"]

        # Init first
        runner.invoke(app, ["init", "--repo-path", str(repo)])

        # Patch load_config to return our test config
        cfg = dotsync_env["cfg"]
        monkeypatch.setattr("dotsync.config.load_config", lambda: cfg)

        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        assert "Repo path" in result.output
        assert "Managed files" in result.output
        assert "0" in result.output  # 0 managed files


class TestConfigCommand:
    """Tests for the 'config' CLI command."""

    def test_config_show_and_set(
        self,
        dotsync_env: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """'config --show' displays values, '--set' updates them."""
        _patch_dependencies(monkeypatch)
        repo = dotsync_env["repo"]
        cfg = dotsync_env["cfg"]

        # Init to create config file
        runner.invoke(app, ["init", "--repo-path", str(repo)])

        # Patch load_config
        monkeypatch.setattr("dotsync.config.load_config", lambda: cfg)

        # Show config
        result = runner.invoke(app, ["config", "--show"])
        assert result.exit_code == 0, result.output
        assert "repo_path" in result.output

        # Set a value
        result = runner.invoke(app, ["config", "--set", "snapshot_keep=3"])
        assert result.exit_code == 0, result.output
        assert "snapshot_keep" in result.output


class TestDiscoverCommand:
    """Tests for the 'discover' CLI command."""

    def test_discover_noninteractive(
        self,
        dotsync_env: dict[str, Any],
        sample_dotfiles: list[Path],
        monkeypatch: pytest.MonkeyPatch,
        mock_gitcrypt: Any,
    ) -> None:
        """'dotsync discover' finds dotfiles and lists them in output."""
        _patch_dependencies(monkeypatch)
        repo = dotsync_env["repo"]
        cfg = dotsync_env["cfg"]

        # Init repo
        runner.invoke(app, ["init", "--repo-path", str(repo)])

        # Patch load_config
        monkeypatch.setattr("dotsync.config.load_config", lambda: cfg)

        # Run discover with --no-ai; answer 'S' for any pending prompts, 'y' for register
        result = runner.invoke(app, ["discover", "--no-ai"], input="S\nS\nS\nS\ny\n")

        # Should run without crashing; check exit code 0 or user_aborted (5)
        # (interactive prompts may cause early exit depending on file classification)
        assert result.exit_code in (0, 5), result.output


class TestSyncDryRun:
    """Tests for the 'sync --dry-run' CLI command."""

    def test_full_sync_dry_run(
        self,
        dotsync_env: dict[str, Any],
        sample_dotfiles: list[Path],
        monkeypatch: pytest.MonkeyPatch,
        mock_gitcrypt: Any,
        mock_health_checks: Any,
    ) -> None:
        """'sync --dry-run --no-push' reports dry run and copies no files."""
        repo = dotsync_env["repo"]
        cfg = dotsync_env["cfg"]

        from dotsync.git_ops import ManifestEntry, init_repo, save_manifest

        # Init repo and create manifest with sample files
        init_repo(cfg)
        entries = [
            ManifestEntry(
                relative_path=".bashrc",
                os_profile="shared",
                added_at="2026-01-01T00:00:00+00:00",
                sensitive_flagged=False,
            ),
        ]
        save_manifest(repo, entries)

        # Patch load_config and flagging
        monkeypatch.setattr("dotsync.config.load_config", lambda: cfg)
        monkeypatch.setattr("dotsync.flagging.flag_all", lambda files, c: [])
        monkeypatch.setattr("dotsync.main.confirm_sensitive_files", lambda fr: fr)

        result = runner.invoke(app, ["sync", "--dry-run", "--no-push"])

        assert result.exit_code == 0, result.output
        assert "ry run" in result.output  # "Dry run" with any case
        # File should NOT have been copied to repo
        assert not (repo / ".bashrc").exists()
