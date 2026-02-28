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


HOME_SCAN_DEPTH = 1
"""$HOME is scanned shallowly — only direct children (dotfiles)."""

KNOWN_CONFIG_SUBDIRS: list[str] = [
    # XDG
    ".config", ".local",
    # Shell
    ".oh-my-zsh", ".zsh", ".bash_it",
    # Editors
    ".vim", ".nvim", ".emacs.d", ".nano",
    # Dev tools
    ".ssh", ".gnupg", ".aws", ".kube", ".docker",
    ".cargo", ".rustup", ".npm", ".nvm", ".pyenv", ".rbenv",
    # Dotfiles repo itself (at depth 1 under $HOME)
    ".git",
]
"""Subdirectories of $HOME that get deep scanning (up to MAX_DEPTH)."""


def config_dirs() -> list[tuple[Path, int]]:
    """Return default scan roots with per-root max depth.

    Each entry is ``(path, max_depth)`` where *max_depth* limits how
    deeply the scanner will recurse into that root.  ``$HOME`` gets a
    shallow scan (HOME_SCAN_DEPTH) while known config subdirectories
    and platform-specific roots receive a deep scan.

    Returns:
        List of ``(Path, max_depth)`` tuples.
    """
    deep_depth = 5  # matches discovery.MAX_DEPTH
    os_name = current_os()
    home = Path.home()

    roots: list[tuple[Path, int]] = [(home, HOME_SCAN_DEPTH)]

    if os_name == "linux":
        for subdir in KNOWN_CONFIG_SUBDIRS:
            d = home / subdir
            if d.exists():
                roots.append((d, deep_depth))

        # XDG_CONFIG_HOME if set and different from ~/.config
        xdg = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
        if xdg != home / ".config" and xdg.exists():
            roots.append((xdg, deep_depth))

    elif os_name == "windows":
        appdata = os.environ.get("APPDATA", "")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        if appdata:
            roots.append((Path(appdata), 4))
        if localappdata:
            roots.append((Path(localappdata), 4))

    return roots
