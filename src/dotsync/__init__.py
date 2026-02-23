"""DotSync — CLI tool to backup, sync, and encrypt configuration files."""

from dotsync.config import (
    ConfigNotFoundError,
    DotSyncConfig,
    default_config,
    load_config,
    save_config,
)
from dotsync.discovery import ConfigFile, discover
from dotsync.logging_setup import setup_logging
from dotsync.platform_utils import config_dirs, current_os, home_dir, is_wsl

__all__ = [
    "ConfigFile",
    "ConfigNotFoundError",
    "DotSyncConfig",
    "default_config",
    "discover",
    "load_config",
    "save_config",
    "setup_logging",
    "current_os",
    "is_wsl",
    "home_dir",
    "config_dirs",
]
