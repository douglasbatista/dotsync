"""Tests for dotsync.sync module."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from dotsync.config import DotSyncConfig
from dotsync.discovery import ConfigFile
from dotsync.flagging import FlagResult
from dotsync.git_ops import ManifestEntry, MANIFEST_FILENAME
from dotsync.sync import (
    Conflict,
    RestoreAction,
    SyncAction,
    detect_conflicts,
    execute_restore,
    execute_sync,
    filter_by_profile,
    plan_restore,
    plan_sync,
    register_new_files,
    transform_paths,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(
    relative_path: str = ".bashrc",
    os_profile: str = "shared",
    sensitive_flagged: bool = False,
) -> ManifestEntry:
    """Create a ManifestEntry with defaults."""
    return ManifestEntry(
        relative_path=relative_path,
        os_profile=os_profile,
        added_at="2025-01-01T00:00:00+00:00",
        sensitive_flagged=sensitive_flagged,
    )


def _cfg(tmp_path: Path) -> DotSyncConfig:
    """Return a DotSyncConfig pointing at tmp_path."""
    return DotSyncConfig(
        repo_path=tmp_path / "repo",
        exclude_patterns=[],
        include_extra=[],
    )


def _config_file(
    tmp_path: Path,
    rel: str = ".bashrc",
    include: bool = True,
    os_profile: str = "shared",
) -> ConfigFile:
    """Create a ConfigFile with a real file on disk."""
    abs_path = tmp_path / "home" / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text("# config", encoding="utf-8")
    return ConfigFile(
        path=Path(rel),
        abs_path=abs_path,
        size_bytes=8,
        include=include,
        reason="test",
        os_profile=os_profile,
    )


# ---------------------------------------------------------------------------
# TestFilterByProfile (Step 5.1)
# ---------------------------------------------------------------------------


class TestFilterByProfile:
    def test_filter_includes_shared_on_linux(self) -> None:
        entries = [_entry(os_profile="shared"), _entry(".vimrc", os_profile="linux")]
        result = filter_by_profile(entries, "linux")
        assert len(result) == 2

    def test_filter_includes_shared_on_windows(self) -> None:
        entries = [_entry(os_profile="shared")]
        result = filter_by_profile(entries, "windows")
        assert len(result) == 1
        assert result[0].os_profile == "shared"

    def test_filter_excludes_windows_entry_on_linux(self) -> None:
        entries = [_entry("AppData/some.json", os_profile="windows")]
        result = filter_by_profile(entries, "linux")
        assert len(result) == 0

    def test_filter_excludes_linux_entry_on_windows(self) -> None:
        entries = [_entry(".config/nvim/init.vim", os_profile="linux")]
        result = filter_by_profile(entries, "windows")
        assert len(result) == 0


# ---------------------------------------------------------------------------
# TestTransformPaths (Step 5.2)
# ---------------------------------------------------------------------------


class TestTransformPaths:
    def test_transform_linux_to_windows_home_path(self) -> None:
        content = 'editor="/home/user/.local/bin/vim"'
        result = transform_paths(
            content, "linux", "windows",
            "/home/user", r"C:\Users\user",
        )
        assert r"C:\Users\user" in result
        assert "/home/user" not in result

    def test_transform_windows_to_linux_home_path(self) -> None:
        content = r'editor="C:\Users\user\.local\bin\vim"'
        result = transform_paths(
            content, "windows", "linux",
            r"C:\Users\user", "/home/user",
        )
        assert "/home/user" in result
        assert r"C:\Users\user" not in result

    def test_transform_no_op_same_os(self) -> None:
        content = 'path="/home/user/.config"'
        result = transform_paths(
            content, "linux", "linux",
            "/home/user", "/home/other",
        )
        assert result == content

    def test_transform_does_not_mangle_urls(self) -> None:
        content = 'url="https://example.com/path"\npath="/home/user/bin"'
        result = transform_paths(
            content, "linux", "windows",
            "/home/user", r"C:\Users\user",
        )
        assert "https://example.com/path" in result


# ---------------------------------------------------------------------------
# TestPlanSync (Step 5.3)
# ---------------------------------------------------------------------------


class TestPlanSync:
    def test_plan_sync_skips_missing_files(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()

        entries = [_entry(".bashrc", os_profile="shared")]
        # .bashrc does not exist in home
        actions = plan_sync(entries, home, repo, "linux")

        assert len(actions) == 1
        assert actions[0].action == "skip_missing"

    def test_plan_sync_marks_copy_for_existing(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()

        # Create the file in home
        bashrc = home / ".bashrc"
        bashrc.write_text("# bash config", encoding="utf-8")

        entries = [_entry(".bashrc", os_profile="shared")]
        actions = plan_sync(entries, home, repo, "linux")

        assert len(actions) == 1
        assert actions[0].action == "copy"
        assert actions[0].source == bashrc


# ---------------------------------------------------------------------------
# TestExecuteSync (Step 5.3)
# ---------------------------------------------------------------------------


class TestExecuteSync:
    def test_execute_sync_dry_run_no_writes(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()

        src = home / ".bashrc"
        src.write_text("# config", encoding="utf-8")
        dest = repo / ".bashrc"

        actions = [
            SyncAction(source=src, destination=dest, action="copy", transformed=False)
        ]
        result = execute_sync(actions, dry_run=True)

        assert len(result) == 1
        assert not dest.exists()

    def test_execute_sync_copies_files(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()

        src = home / ".bashrc"
        src.write_text("# bash config", encoding="utf-8")
        dest = repo / ".bashrc"

        actions = [
            SyncAction(source=src, destination=dest, action="copy", transformed=False)
        ]
        execute_sync(actions)

        assert dest.exists()
        assert dest.read_text(encoding="utf-8") == "# bash config"

    def test_execute_sync_skips_non_copy_actions(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()

        src = home / ".bashrc"
        dest = repo / ".bashrc"

        actions = [
            SyncAction(
                source=src, destination=dest,
                action="skip_missing", transformed=False,
            )
        ]
        execute_sync(actions)

        assert not dest.exists()


# ---------------------------------------------------------------------------
# TestPlanRestore (Step 5.4)
# ---------------------------------------------------------------------------


class TestPlanRestore:
    def test_plan_restore_skips_file_not_in_repo(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()

        entries = [_entry(".bashrc", os_profile="shared")]
        # .bashrc doesn't exist in repo
        actions = plan_restore(entries, home, repo, "linux")

        assert len(actions) == 1
        assert actions[0].action == "skip_missing_in_repo"

    def test_plan_restore_skips_wrong_profile(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()

        entries = [_entry("AppData/app.json", os_profile="windows")]
        actions = plan_restore(entries, home, repo, "linux")

        assert len(actions) == 1
        assert actions[0].action == "skip_profile"


# ---------------------------------------------------------------------------
# TestExecuteRestore (Step 5.4)
# ---------------------------------------------------------------------------


class TestExecuteRestore:
    def test_execute_restore_creates_parent_dirs(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        home = tmp_path / "home"
        home.mkdir()

        # Create source file in repo (nested path)
        src = repo / ".config" / "nvim" / "init.vim"
        src.parent.mkdir(parents=True)
        src.write_text("set number", encoding="utf-8")

        dest = home / ".config" / "nvim" / "init.vim"

        actions = [
            RestoreAction(
                source=src, destination=dest,
                action="restore", transformed=False,
            )
        ]
        execute_restore(actions)

        assert dest.exists()
        assert dest.read_text(encoding="utf-8") == "set number"

    def test_execute_restore_dry_run_no_writes(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        home = tmp_path / "home"
        home.mkdir()

        src = repo / ".bashrc"
        src.write_text("# config", encoding="utf-8")
        dest = home / ".bashrc"

        actions = [
            RestoreAction(
                source=src, destination=dest,
                action="restore", transformed=False,
            )
        ]
        result = execute_restore(actions, dry_run=True)

        assert len(result) == 1
        assert not dest.exists()

    def test_execute_restore_applies_reverse_transform(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        home = tmp_path / "home"
        home.mkdir()

        src = repo / ".bashrc"
        src.write_text(
            'export EDITOR="/home/user/.local/bin/vim"',
            encoding="utf-8",
        )
        dest = home / ".bashrc"

        actions = [
            RestoreAction(
                source=src, destination=dest,
                action="restore", transformed=True,
            )
        ]
        execute_restore(
            actions,
            source_os="linux",
            target_os="windows",
            source_home="/home/user",
            target_home=r"C:\Users\user",
        )

        assert dest.exists()
        content = dest.read_text(encoding="utf-8")
        assert r"C:\Users\user" in content


# ---------------------------------------------------------------------------
# TestRegisterNewFiles (Step 5.5)
# ---------------------------------------------------------------------------


class TestRegisterNewFiles:
    def test_register_copies_to_repo(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        repo = tmp_path / "repo"
        repo.mkdir()
        # Write empty manifest
        (repo / MANIFEST_FILENAME).write_text("[]", encoding="utf-8")

        cfg = _cfg(tmp_path)
        cf = _config_file(tmp_path, ".bashrc", include=True)
        fr = FlagResult(config_file=cf, requires_confirmation=False)

        entries = register_new_files([cf], [fr], repo, home, cfg)

        assert len(entries) == 1
        assert (repo / ".bashrc").exists()

    def test_register_adds_to_manifest(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / MANIFEST_FILENAME).write_text("[]", encoding="utf-8")

        cfg = _cfg(tmp_path)
        cf = _config_file(tmp_path, ".vimrc", include=True)
        fr = FlagResult(config_file=cf, requires_confirmation=False)

        register_new_files([cf], [fr], repo, home, cfg)

        manifest_data = json.loads(
            (repo / MANIFEST_FILENAME).read_text(encoding="utf-8")
        )
        assert len(manifest_data) == 1
        assert manifest_data[0]["relative_path"] == ".vimrc"

    def test_register_propagates_sensitive_flag(self, tmp_path: Path) -> None:
        """sensitive_flagged should reflect ConfigFile.sensitive."""
        home = tmp_path / "home"
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / MANIFEST_FILENAME).write_text("[]", encoding="utf-8")

        cfg = _cfg(tmp_path)
        cf = _config_file(tmp_path, ".env_config", include=True)
        cf.sensitive = True
        fr = FlagResult(config_file=cf, requires_confirmation=False)

        entries = register_new_files([cf], [fr], repo, home, cfg)

        assert len(entries) == 1
        assert entries[0].sensitive_flagged is True

        manifest_data = json.loads(
            (repo / MANIFEST_FILENAME).read_text(encoding="utf-8")
        )
        assert manifest_data[0]["sensitive_flagged"] is True

    def test_register_sensitive_false_by_default(self, tmp_path: Path) -> None:
        """Non-sensitive file should have sensitive_flagged=False in manifest."""
        home = tmp_path / "home"
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / MANIFEST_FILENAME).write_text("[]", encoding="utf-8")

        cfg = _cfg(tmp_path)
        cf = _config_file(tmp_path, ".bashrc", include=True)
        fr = FlagResult(config_file=cf, requires_confirmation=False)

        entries = register_new_files([cf], [fr], repo, home, cfg)

        assert len(entries) == 1
        assert entries[0].sensitive_flagged is False

    def test_register_dry_run_no_writes(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / MANIFEST_FILENAME).write_text("[]", encoding="utf-8")

        cfg = _cfg(tmp_path)
        cf = _config_file(tmp_path, ".bashrc", include=True)
        fr = FlagResult(config_file=cf, requires_confirmation=False)

        entries = register_new_files([cf], [fr], repo, home, cfg, dry_run=True)

        assert len(entries) == 1
        assert not (repo / ".bashrc").exists()
        # Manifest should be unchanged
        manifest_data = json.loads(
            (repo / MANIFEST_FILENAME).read_text(encoding="utf-8")
        )
        assert len(manifest_data) == 0


# ---------------------------------------------------------------------------
# TestDetectConflicts (Step 5.6)
# ---------------------------------------------------------------------------


class TestDetectConflicts:
    def test_conflict_detected_when_both_modified(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()

        # Set last_sync to the past
        last_sync = datetime.now().astimezone() - timedelta(hours=1)

        # Create files (modified "now", which is after last_sync)
        local = home / ".bashrc"
        local.write_text("local version", encoding="utf-8")
        repo_file = repo / ".bashrc"
        repo_file.write_text("repo version", encoding="utf-8")

        entries = [_entry(".bashrc")]
        conflicts = detect_conflicts(entries, home, repo, last_sync)

        assert len(conflicts) == 1
        assert conflicts[0].relative_path == ".bashrc"

    def test_no_conflict_when_only_repo_modified(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()

        # Create local file first, then set last_sync after it
        local = home / ".bashrc"
        local.write_text("local version", encoding="utf-8")

        # Record a time after local was written
        time.sleep(0.05)
        last_sync = datetime.now().astimezone()
        time.sleep(0.05)

        # Now create repo file (after last_sync)
        repo_file = repo / ".bashrc"
        repo_file.write_text("repo version", encoding="utf-8")

        entries = [_entry(".bashrc")]
        conflicts = detect_conflicts(entries, home, repo, last_sync)

        assert len(conflicts) == 0

    def test_no_conflict_when_only_local_modified(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()

        # Create repo file first, then set last_sync after it
        repo_file = repo / ".bashrc"
        repo_file.write_text("repo version", encoding="utf-8")

        # Record a time after repo file was written
        time.sleep(0.05)
        last_sync = datetime.now().astimezone()
        time.sleep(0.05)

        # Now create local file (after last_sync)
        local = home / ".bashrc"
        local.write_text("local version", encoding="utf-8")

        entries = [_entry(".bashrc")]
        conflicts = detect_conflicts(entries, home, repo, last_sync)

        assert len(conflicts) == 0
