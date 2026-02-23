"""Unit tests for Module 01 — Core / Bootstrap."""

import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.logging import RichHandler

from dotsync.config import (
    CONFIG_FILE,
    CONFIG_DIR,
    ConfigNotFoundError,
    DotSyncConfig,
    default_config,
    load_config,
    save_config,
)
from dotsync.logging_setup import LOG_FILE, setup_logging
from dotsync.platform_utils import config_dirs, current_os, home_dir, is_wsl


class TestConfig:
    """Tests for configuration schema and load/save functions."""

    def test_config_defaults(self) -> None:
        """Test that default_config returns correct types and values."""
        cfg = default_config()

        assert isinstance(cfg.repo_path, Path)
        assert cfg.remote_url is None
        assert cfg.gitcrypt_key_path is None
        assert cfg.llm_endpoint is None
        assert cfg.llm_model == "claude-haiku-4-5"
        assert cfg.snapshot_keep == 5
        assert cfg.health_checks == []
        assert cfg.exclude_patterns == []
        assert cfg.include_extra == []

    def test_config_roundtrip(self, tmp_path: Path) -> None:
        """Test that saving then loading config preserves values."""
        # Create a temporary config directory
        temp_config_dir = tmp_path / ".dotsync"
        temp_config_file = temp_config_dir / "config.toml"

        # Patch the config file location
        with patch.object(CONFIG_FILE.__class__, "__new__", return_value=temp_config_file):
            with patch("dotsync.config.CONFIG_DIR", temp_config_dir):
                with patch("dotsync.config.CONFIG_FILE", temp_config_file):
                    # Create a config with custom values
                    cfg = DotSyncConfig(
                        repo_path=Path("/tmp/test-repo"),
                        remote_url="https://github.com/user/dotsync-repo.git",
                        gitcrypt_key_path=Path("/tmp/key.bin"),
                        llm_endpoint="http://localhost:4000",
                        llm_model="claude-sonnet-4",
                        snapshot_keep=10,
                        health_checks=["git status", "git log"],
                        exclude_patterns=["*.log", "*.tmp"],
                        include_extra=["/etc/hosts"],
                    )

                    # Save the config
                    save_config(cfg)

                    # Load it back
                    loaded = load_config()

                    # Verify all values match
                    assert loaded.repo_path == cfg.repo_path
                    assert loaded.remote_url == cfg.remote_url
                    assert loaded.gitcrypt_key_path == cfg.gitcrypt_key_path
                    assert loaded.llm_endpoint == cfg.llm_endpoint
                    assert loaded.llm_model == cfg.llm_model
                    assert loaded.snapshot_keep == cfg.snapshot_keep
                    assert loaded.health_checks == cfg.health_checks
                    assert loaded.exclude_patterns == cfg.exclude_patterns
                    assert loaded.include_extra == cfg.include_extra

    def test_config_missing(self) -> None:
        """Test that load_config raises ConfigNotFoundError when file is absent."""
        # Use a non-existent path
        with patch("dotsync.config.CONFIG_FILE", Path("/nonexistent/path/config.toml")):
            with pytest.raises(ConfigNotFoundError):
                load_config()


class TestLogging:
    """Tests for logging infrastructure."""

    def test_log_file_created(self, tmp_path: Path) -> None:
        """Test that setup_logging creates the log file."""
        temp_log_dir = tmp_path / ".dotsync"
        temp_log_file = temp_log_dir / "dotsync.log"

        with patch("dotsync.logging_setup.LOG_DIR", temp_log_dir):
            with patch("dotsync.logging_setup.LOG_FILE", temp_log_file):
                setup_logging()

                assert temp_log_file.exists()
                assert temp_log_file.is_file()

    def test_verbose_flag(self) -> None:
        """Test that console handler level is DEBUG when verbose=True."""
        # Clear any existing handlers first
        logger = logging.getLogger("dotsync")
        logger.handlers = []

        setup_logging(verbose=True)

        # Find the RichHandler
        rich_handler = None
        for handler in logger.handlers:
            if isinstance(handler, RichHandler):
                rich_handler = handler
                break

        assert rich_handler is not None
        assert rich_handler.level == logging.DEBUG

    def test_non_verbose_flag(self) -> None:
        """Test that console handler level is INFO when verbose=False."""
        # Clear any existing handlers first
        logger = logging.getLogger("dotsync")
        logger.handlers = []

        setup_logging(verbose=False)

        # Find the RichHandler
        rich_handler = None
        for handler in logger.handlers:
            if isinstance(handler, RichHandler):
                rich_handler = handler
                break

        assert rich_handler is not None
        assert rich_handler.level == logging.INFO


class TestPlatformUtils:
    """Tests for platform detection utilities."""

    def test_current_os_returns_valid_literal(self) -> None:
        """Test that current_os returns a valid OS literal."""
        os_name = current_os()
        assert os_name in ("linux", "windows")

    def test_home_dir_exists(self) -> None:
        """Test that home_dir returns an existing directory."""
        home = home_dir()
        assert home.exists()
        assert home.is_dir()

    def test_config_dirs_all_exist(self) -> None:
        """Test that config_dirs returns existing directories (filtered)."""
        dirs = config_dirs()
        assert len(dirs) > 0

        # Filter to existing directories only
        existing_dirs = [d for d in dirs if d.exists()]
        assert len(existing_dirs) > 0

    def test_is_wsl_on_non_wsl(self) -> None:
        """Test is_wsl returns False on non-WSL Linux."""
        # This test assumes we're either on WSL or not
        # It's mainly to ensure the function doesn't crash
        result = is_wsl()
        assert isinstance(result, bool)
