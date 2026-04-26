"""Shared fixtures for integration and end-to-end tests."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from dotsync.config import DotSyncConfig


@pytest.fixture()
def dotsync_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Core isolation fixture redirecting all filesystem state to tmp_path.

    Creates fake HOME, repo, and config directories under tmp_path.
    Patches all module-level path constants so no real filesystem is touched.

    Returns:
        Dict with ``home``, ``repo``, ``config_dir`` Paths and a ``cfg``
        DotSyncConfig pointing at the temporary layout.
    """
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    config_dir = tmp_path / "dotsync"

    home.mkdir()
    repo.mkdir()
    config_dir.mkdir()

    # Patch module-level constants
    monkeypatch.setattr("dotsync.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("dotsync.config.CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.setattr("dotsync.snapshot.SNAPSHOTS_DIR", config_dir / "snapshots")
    monkeypatch.setattr(
        "dotsync.discovery.CLASSIFICATION_CACHE_FILE",
        config_dir / "classification_cache.json",
    )
    monkeypatch.setattr(
        "dotsync.flagging.SENSITIVITY_CACHE_FILE",
        config_dir / "sensitivity_cache.json",
    )

    # Patch HOME env var
    monkeypatch.setenv("HOME", str(home))

    # Patch home_dir() and config_dirs() in discovery (where they are imported)
    monkeypatch.setattr("dotsync.discovery.home_dir", lambda: home)
    monkeypatch.setattr("dotsync.discovery.config_dirs", lambda: [(home, 5)])

    # Patch home_dir() in platform_utils (used by main.py and others)
    monkeypatch.setattr("dotsync.platform_utils.home_dir", lambda: home)

    cfg = DotSyncConfig(repo_path=repo)

    return {
        "home": home,
        "repo": repo,
        "config_dir": config_dir,
        "cfg": cfg,
    }


@pytest.fixture()
def sample_dotfiles(dotsync_env: dict[str, Any]) -> list[Path]:
    """Create realistic dotfiles under fake HOME.

    Returns:
        List of absolute paths to created files.
    """
    home = dotsync_env["home"]
    files = {
        ".bashrc": "# ~/.bashrc\nalias ll='ls -la'\nexport PATH=$PATH:~/bin\n",
        ".gitconfig": "[user]\n\tname = Test User\n\temail = test@example.com\n",
        ".config/nvim/init.vim": "set number\nset relativenumber\n",
        ".vimrc": "syntax on\nset tabstop=4\n",
    }

    created: list[Path] = []
    for rel, content in files.items():
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created.append(path)

    return created


@pytest.fixture()
def mock_health_checks(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch subprocess.run in dotsync.health to return success for all checks."""
    mock = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=["mock"], returncode=0, stdout="ok\n", stderr="",
        ),
    )
    monkeypatch.setattr("dotsync.health.subprocess.run", mock)
    return mock
