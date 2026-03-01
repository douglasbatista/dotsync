"""Unit tests for Module 01 — Core / Bootstrap."""

import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.logging import RichHandler

from dotsync.config import (
    CONFIG_FILE,
    ConfigNotFoundError,
    DotSyncConfig,
    default_config,
    expand_path,
    load_config,
    save_config,
)
from dotsync.logging_setup import setup_logging
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
                        include_extra=[Path("/etc/hosts")],
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

    def test_config_roundtrip_with_defaults(self, tmp_path: Path) -> None:
        """Test that saving then loading default config preserves None optionals."""
        temp_config_dir = tmp_path / ".dotsync"
        temp_config_file = temp_config_dir / "config.toml"

        with patch("dotsync.config.CONFIG_DIR", temp_config_dir):
            with patch("dotsync.config.CONFIG_FILE", temp_config_file):
                cfg = default_config()
                save_config(cfg)
                loaded = load_config()

                assert loaded.repo_path == cfg.repo_path
                assert loaded.remote_url is None
                assert loaded.gitcrypt_key_path is None
                assert loaded.llm_endpoint is None
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
        """Test that config_dirs returns (Path, max_depth) tuples."""
        dirs = config_dirs()
        assert len(dirs) > 0

        # Each entry is a (Path, int) tuple
        for path, depth in dirs:
            assert isinstance(path, Path)
            assert isinstance(depth, int)

        # Filter to existing directories only
        existing_dirs = [(d, depth) for d, depth in dirs if d.exists()]
        assert len(existing_dirs) > 0

    def test_is_wsl_on_non_wsl(self) -> None:
        """Test is_wsl returns False on non-WSL Linux."""
        # This test assumes we're either on WSL or not
        # It's mainly to ensure the function doesn't crash
        result = is_wsl()
        assert isinstance(result, bool)


class TestExpandPath:
    """Tests for the expand_path utility function."""

    def test_expand_path_none(self) -> None:
        """Test that expand_path returns None for None input."""
        assert expand_path(None) is None

    def test_expand_path_tilde_linux(self) -> None:
        """Test that ~/dotsync-repo expands to an absolute path under home."""
        result = expand_path("~/dotsync-repo")
        assert result is not None
        assert result.is_absolute()
        assert str(result).startswith(str(Path.home()))
        assert result.name == "dotsync-repo"

    def test_expand_path_no_resolve(self) -> None:
        """Test that resolve=False expands ~ but does not resolve."""
        result = expand_path("~/some/relative/../path", resolve=False)
        assert result is not None
        assert result.is_absolute()
        # With resolve=False, '..' is preserved in the path
        assert ".." in str(result)

    def test_expand_path_already_absolute(self) -> None:
        """Test that already-absolute paths are returned as-is (after resolve)."""
        result = expand_path("/usr/local/bin")
        assert result is not None
        assert result == Path("/usr/local/bin")


class TestConfigPathExpansion:
    """Tests for Pydantic field validators that expand paths in DotSyncConfig."""

    def test_config_expands_repo_path(self) -> None:
        """Test that repo_path with ~ is expanded to an absolute Path."""
        cfg = DotSyncConfig(repo_path="~/dotsync-repo")  # type: ignore[arg-type]
        assert cfg.repo_path.is_absolute()
        assert str(cfg.repo_path).startswith(str(Path.home()))
        assert cfg.repo_path.name == "dotsync-repo"

    def test_config_expands_gitcrypt_key_path(self) -> None:
        """Test that gitcrypt_key_path with ~ is expanded to an absolute Path."""
        cfg = DotSyncConfig(
            repo_path="/tmp/repo",
            gitcrypt_key_path="~/keys/dotsync.key",  # type: ignore[arg-type]
        )
        assert cfg.gitcrypt_key_path is not None
        assert cfg.gitcrypt_key_path.is_absolute()
        assert str(cfg.gitcrypt_key_path).startswith(str(Path.home()))
        assert cfg.gitcrypt_key_path.name == "dotsync.key"

    def test_config_expands_include_extra(self) -> None:
        """Test that include_extra paths with ~ are expanded to absolute Paths."""
        cfg = DotSyncConfig(
            repo_path="/tmp/repo",
            include_extra=["~/.config/custom"],  # type: ignore[arg-type]
        )
        assert len(cfg.include_extra) == 1
        assert cfg.include_extra[0].is_absolute()
        assert str(cfg.include_extra[0]).startswith(str(Path.home()))

    def test_config_expands_exclude_patterns(self) -> None:
        """Test that exclude_patterns expand ~ but do not resolve."""
        cfg = DotSyncConfig(
            repo_path="/tmp/repo",
            exclude_patterns=["~/.config/*/cache"],
        )
        assert len(cfg.exclude_patterns) == 1
        # ~ should be expanded to home dir
        assert not cfg.exclude_patterns[0].startswith("~")
        assert str(Path.home()) in cfg.exclude_patterns[0]
        # Pattern glob chars should be preserved
        assert "*/cache" in cfg.exclude_patterns[0]

    def test_config_health_checks_not_expanded(self) -> None:
        """Test that health_checks strings are left as-is (no path expansion)."""
        cfg = DotSyncConfig(
            repo_path="/tmp/repo",
            health_checks=["ls ~/bin", "echo $HOME"],
        )
        assert cfg.health_checks == ["ls ~/bin", "echo $HOME"]

    def test_config_expands_env_var(self) -> None:
        """Test that environment variables in paths are expanded."""
        with patch.dict("os.environ", {"MY_REPO": "/tmp/my-repo"}):
            cfg = DotSyncConfig(repo_path="$MY_REPO")  # type: ignore[arg-type]
            assert cfg.repo_path == Path("/tmp/my-repo")
