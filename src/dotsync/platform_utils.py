"""Platform detection utilities for DotSync."""

import os
import sys
from pathlib import Path
from typing import Literal


def current_os() -> Literal["linux", "windows"]:
    """Detect the current operating system.

    Returns:
        Literal["linux", "windows"]: The current OS.

    Raises:
        RuntimeError: If the OS is not supported.
    """
    if sys.platform.startswith("linux"):
        return "linux"
    elif sys.platform == "win32":
        return "windows"
    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")


def is_wsl() -> bool:
    """Check if running under Windows Subsystem for Linux.

    Returns:
        bool: True if running under WSL, False otherwise.
    """
    if sys.platform != "linux":
        return False

    proc_version = Path("/proc/version")
    if not proc_version.exists():
        return False

    try:
        content = proc_version.read_text(encoding="utf-8")
        return "Microsoft" in content
    except (OSError, IOError):
        return False


def home_dir() -> Path:
    """Return the home directory for the current platform.

    Returns:
        Path: The home directory path.
    """
    return Path.home()


def config_dirs() -> list[Path]:
    """Return default scan roots for configuration files.

    Returns:
        list[Path]: List of directories to scan for config files.
    """
    os_name = current_os()

    if os_name == "linux":
        return [Path.home(), Path.home() / ".config"]
    elif os_name == "windows":
        appdata = os.environ.get("APPDATA", "")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        dirs = [Path.home()]
        if appdata:
            dirs.append(Path(appdata))
        if localappdata:
            dirs.append(Path(localappdata))
        return dirs
    else:
        return [Path.home()]
