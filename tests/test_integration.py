"""Integration tests — cross-module workflows with real filesystem I/O."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
import pytest

from dotsync.config import DotSyncConfig
from dotsync.discovery import ConfigFile, discover
from dotsync.flagging import FlagResult, flag_all
from dotsync.git_ops import ManifestEntry, init_repo, load_manifest, save_manifest
from dotsync.health import (
    HealthCheck,
    HealthCheckFailedError,
    HealthCheckResult,
    check_and_rollback_if_needed,
    post_operation_checks,
)
from dotsync.snapshot import apply_retention, create_snapshot, list_snapshots, rollback
from dotsync.sync import (
    execute_restore,
    execute_sync,
    plan_restore,
    plan_sync,
    register_new_files,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Config → Discovery pipeline
# ---------------------------------------------------------------------------


class TestConfigDiscoveryPipeline:
    """Tests for config → discovery integration."""

    def test_discover_finds_home_dotfiles(
        self,
        dotsync_env: dict[str, Any],
        sample_dotfiles: list[Path],
    ) -> None:
        """Dotfiles written under fake HOME are discovered and classified."""
        cfg = dotsync_env["cfg"]
        files = discover(cfg)

        found_paths = {str(f.path) for f in files}
        assert ".bashrc" in found_paths
        assert ".gitconfig" in found_paths

        # Heuristic should classify home dotfiles as include
        bashrc = next(f for f in files if str(f.path) == ".bashrc")
        assert bashrc.include is True
        assert bashrc.reason == "home dotfile"

    def test_discover_respects_exclude_patterns(
        self,
        dotsync_env: dict[str, Any],
        sample_dotfiles: list[Path],
    ) -> None:
        """Files matching exclude_patterns are excluded from results."""
        cfg: DotSyncConfig = dotsync_env["cfg"]
        cfg.exclude_patterns = [".vimrc"]

        files = discover(cfg)

        vimrc_results = [f for f in files if str(f.path) == ".vimrc"]
        assert len(vimrc_results) == 1
        assert vimrc_results[0].include is False
        assert vimrc_results[0].reason == "user_excluded"

    def test_discover_skips_repo_path(
        self,
        dotsync_env: dict[str, Any],
        sample_dotfiles: list[Path],
    ) -> None:
        """The dotfile repo directory itself is excluded from scan results."""
        home = dotsync_env["home"]
        cfg: DotSyncConfig = dotsync_env["cfg"]

        # Place repo inside home so scanner would otherwise find it
        repo_inside_home = home / "my-dotfiles-repo"
        repo_inside_home.mkdir()
        (repo_inside_home / ".gitconfig").write_text("[core]\n", encoding="utf-8")
        cfg.repo_path = repo_inside_home

        files = discover(cfg)

        # No file from inside the repo dir should appear
        repo_abs = repo_inside_home.resolve()
        for f in files:
            assert not f.abs_path.resolve().is_relative_to(repo_abs), (
                f"File inside repo_path was discovered: {f.abs_path}"
            )


# ---------------------------------------------------------------------------
# Discovery → Flagging → Registration
# ---------------------------------------------------------------------------


class TestDiscoveryFlaggingRegistration:
    """Tests for discovery → flagging → registration integration."""

    def test_flagging_detects_secrets_in_discovered_files(
        self,
        dotsync_env: dict[str, Any],
    ) -> None:
        """A file containing a GitHub token is flagged as requiring confirmation."""
        home = dotsync_env["home"]
        cfg: DotSyncConfig = dotsync_env["cfg"]

        secret_file = home / ".env_config"
        secret_file.write_text(
            "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789\n",
            encoding="utf-8",
        )

        files = discover(cfg)
        env_cf = next(f for f in files if str(f.path) == ".env_config")
        env_cf.include = True  # Force include for flagging

        results = flag_all([env_cf], cfg)

        assert len(results) == 1
        assert results[0].requires_confirmation is True
        assert any(m.pattern_name == "github_token" for m in results[0].matches)

    def test_clean_file_passes_flagging(
        self,
        dotsync_env: dict[str, Any],
        sample_dotfiles: list[Path],
    ) -> None:
        """A normal .bashrc with no secrets passes flagging without matches."""
        cfg: DotSyncConfig = dotsync_env["cfg"]

        files = discover(cfg)
        bashrc = next(f for f in files if str(f.path) == ".bashrc")
        assert bashrc.include is True

        results = flag_all([bashrc], cfg)

        assert len(results) == 1
        assert results[0].requires_confirmation is False
        assert results[0].matches == []

    def test_register_propagates_sensitive_flag(
        self,
        dotsync_env: dict[str, Any],
        mock_gitcrypt: Any,
    ) -> None:
        """Registering a flagged file sets sensitive_flagged=True in manifest."""
        home = dotsync_env["home"]
        repo = dotsync_env["repo"]
        cfg: DotSyncConfig = dotsync_env["cfg"]

        # Init repo so manifest exists
        init_repo(cfg)

        secret_file = home / ".secret_rc"
        secret_file.write_text(
            "export API_KEY=sk-abc12345678901234567890abcdefgh\n",
            encoding="utf-8",
        )

        cf = ConfigFile(
            path=Path(".secret_rc"),
            abs_path=secret_file,
            size_bytes=secret_file.stat().st_size,
            include=True,
            sensitive=True,
            reason="home dotfile",
            os_profile="shared",
        )

        # Simulate flagging result: confirmed for inclusion, has matches
        fr = FlagResult(
            config_file=cf,
            matches=[],
            ai_flagged=False,
            requires_confirmation=False,
        )

        entries = register_new_files([cf], [fr], repo, home, cfg)

        assert len(entries) == 1
        assert entries[0].sensitive_flagged is True

        # Verify manifest on disk
        manifest = load_manifest(repo)
        assert any(e.sensitive_flagged and e.relative_path == ".secret_rc" for e in manifest)


# ---------------------------------------------------------------------------
# Sync pipeline
# ---------------------------------------------------------------------------


class TestSyncPipeline:
    """Tests for the sync (home → repo) pipeline."""

    def _setup_manifest(
        self, home: Path, repo: Path, cfg: DotSyncConfig, files: dict[str, str],
    ) -> list[ManifestEntry]:
        """Write files under home, init repo, and create manifest entries."""
        init_repo(cfg)
        entries: list[ManifestEntry] = []
        for rel, content in files.items():
            path = home / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            entry = ManifestEntry(
                relative_path=rel,
                os_profile="shared",
                added_at="2026-01-01T00:00:00+00:00",
                sensitive_flagged=False,
            )
            entries.append(entry)
        save_manifest(repo, entries)
        return entries

    def test_sync_copies_files_to_repo(
        self,
        dotsync_env: dict[str, Any],
        mock_gitcrypt: Any,
    ) -> None:
        """After plan+execute sync, files exist in repo with correct content."""
        home = dotsync_env["home"]
        repo = dotsync_env["repo"]
        cfg = dotsync_env["cfg"]

        entries = self._setup_manifest(home, repo, cfg, {
            ".bashrc": "alias ll='ls -la'\n",
            ".gitconfig": "[user]\n\tname = Test\n",
        })

        actions = plan_sync(entries, home, repo, "linux")
        execute_sync(actions)

        assert (repo / ".bashrc").read_text(encoding="utf-8") == "alias ll='ls -la'\n"
        assert (repo / ".gitconfig").read_text(encoding="utf-8") == "[user]\n\tname = Test\n"

    def test_sync_updates_manifest(
        self,
        dotsync_env: dict[str, Any],
        mock_gitcrypt: Any,
    ) -> None:
        """After sync, manifest entries match synced files."""
        home = dotsync_env["home"]
        repo = dotsync_env["repo"]
        cfg = dotsync_env["cfg"]

        entries = self._setup_manifest(home, repo, cfg, {".bashrc": "# test\n"})

        actions = plan_sync(entries, home, repo, "linux")
        execute_sync(actions)

        manifest = load_manifest(repo)
        assert len(manifest) == 1
        assert manifest[0].relative_path == ".bashrc"

    def test_sync_dry_run_no_changes(
        self,
        dotsync_env: dict[str, Any],
        mock_gitcrypt: Any,
    ) -> None:
        """Dry-run sync does not copy files to repo."""
        home = dotsync_env["home"]
        repo = dotsync_env["repo"]
        cfg = dotsync_env["cfg"]

        entries = self._setup_manifest(home, repo, cfg, {".bashrc": "# test\n"})

        actions = plan_sync(entries, home, repo, "linux")
        execute_sync(actions, dry_run=True)

        assert not (repo / ".bashrc").exists()


# ---------------------------------------------------------------------------
# Restore pipeline
# ---------------------------------------------------------------------------


class TestRestorePipeline:
    """Tests for the restore (repo → home) pipeline."""

    def test_restore_copies_from_repo_to_home(
        self,
        dotsync_env: dict[str, Any],
        mock_gitcrypt: Any,
    ) -> None:
        """Restore overwrites local files with repo content."""
        home = dotsync_env["home"]
        repo = dotsync_env["repo"]
        cfg = dotsync_env["cfg"]
        init_repo(cfg)

        # Write file to home and sync to repo
        bashrc = home / ".bashrc"
        bashrc.write_text("original content\n", encoding="utf-8")

        entry = ManifestEntry(
            relative_path=".bashrc",
            os_profile="shared",
            added_at="2026-01-01T00:00:00+00:00",
            sensitive_flagged=False,
        )
        save_manifest(repo, [entry])

        # Sync to repo
        actions = plan_sync([entry], home, repo, "linux")
        execute_sync(actions)

        # Modify the local file
        bashrc.write_text("modified content\n", encoding="utf-8")

        # Restore from repo
        restore_actions = plan_restore([entry], home, repo, "linux")
        execute_restore(restore_actions)

        assert bashrc.read_text(encoding="utf-8") == "original content\n"

    def test_restore_with_path_transform(
        self,
        dotsync_env: dict[str, Any],
        mock_gitcrypt: Any,
    ) -> None:
        """Restore transforms paths when source and target OS differ."""
        home = dotsync_env["home"]
        repo = dotsync_env["repo"]
        cfg = dotsync_env["cfg"]
        init_repo(cfg)

        # Write a config file with a Linux home path in a value position
        linux_home = "/home/testuser"
        content = 'editor="/home/testuser/.local/bin/nvim"\n'
        repo_file = repo / ".bashrc"
        repo_file.write_text(content, encoding="utf-8")

        entry = ManifestEntry(
            relative_path=".bashrc",
            os_profile="shared",
            added_at="2026-01-01T00:00:00+00:00",
            sensitive_flagged=False,
        )
        save_manifest(repo, [entry])

        # Plan restore
        restore_actions = plan_restore([entry], home, repo, "windows")
        # Mark as transformed for the path transform to apply
        for a in restore_actions:
            if a.action == "restore":
                a.transformed = True

        windows_home = r"C:\Users\TestUser"
        execute_restore(
            restore_actions,
            source_os="linux",
            target_os="windows",
            source_home=linux_home,
            target_home=windows_home,
        )

        restored = (home / ".bashrc").read_text(encoding="utf-8")
        assert windows_home in restored
        assert linux_home not in restored


# ---------------------------------------------------------------------------
# Snapshot → Rollback
# ---------------------------------------------------------------------------


class TestSnapshotRollback:
    """Tests for snapshot creation and rollback."""

    def test_snapshot_and_rollback_restores_files(
        self,
        dotsync_env: dict[str, Any],
    ) -> None:
        """Create snapshot, modify files, rollback restores original content."""
        home = dotsync_env["home"]

        bashrc = home / ".bashrc"
        bashrc.write_text("original\n", encoding="utf-8")

        entry = ManifestEntry(
            relative_path=".bashrc",
            os_profile="shared",
            added_at="2026-01-01T00:00:00+00:00",
            sensitive_flagged=False,
        )

        snap = create_snapshot([entry], home, trigger="sync", keep=5)

        # Modify file
        bashrc.write_text("modified\n", encoding="utf-8")
        assert bashrc.read_text(encoding="utf-8") == "modified\n"

        # Rollback
        restored = rollback(snap.id, home)

        assert len(restored) == 1
        assert bashrc.read_text(encoding="utf-8") == "original\n"

    def test_snapshot_retention_limits(
        self,
        dotsync_env: dict[str, Any],
    ) -> None:
        """Retention policy deletes oldest snapshots beyond keep limit."""
        home = dotsync_env["home"]

        bashrc = home / ".bashrc"
        bashrc.write_text("test\n", encoding="utf-8")

        entry = ManifestEntry(
            relative_path=".bashrc",
            os_profile="shared",
            added_at="2026-01-01T00:00:00+00:00",
            sensitive_flagged=False,
        )

        # Create snapshots with slight delays to get unique IDs
        snap_ids: list[str] = []
        for i in range(4):
            bashrc.write_text(f"version {i}\n", encoding="utf-8")
            snap = create_snapshot([entry], home, trigger="sync", keep=0)
            snap_ids.append(snap.id)
            time.sleep(1.1)  # ensure unique second-resolution timestamps

        # Now apply retention with keep=2
        deleted = apply_retention(keep=2)

        remaining = list_snapshots()
        assert len(remaining) == 2
        assert len(deleted) == 2


# ---------------------------------------------------------------------------
# Health → Auto-rollback
# ---------------------------------------------------------------------------


class TestHealthAutoRollback:
    """Tests for health checks and automatic rollback."""

    def test_health_check_passes_after_sync(
        self,
        dotsync_env: dict[str, Any],
        mock_health_checks: Any,
    ) -> None:
        """post_operation_checks completes without error when all checks pass."""
        home = dotsync_env["home"]
        cfg = dotsync_env["cfg"]

        bashrc = home / ".bashrc"
        bashrc.write_text("test\n", encoding="utf-8")
        entry = ManifestEntry(
            relative_path=".bashrc",
            os_profile="shared",
            added_at="2026-01-01T00:00:00+00:00",
            sensitive_flagged=False,
        )
        snap = create_snapshot([entry], home, trigger="sync", keep=5)

        # Should not raise
        post_operation_checks(cfg, snap.id, home, operation="sync")

    def test_health_failure_triggers_rollback(
        self,
        dotsync_env: dict[str, Any],
    ) -> None:
        """A failing health check triggers rollback restoring the snapshot."""
        home = dotsync_env["home"]

        bashrc = home / ".bashrc"
        bashrc.write_text("before sync\n", encoding="utf-8")

        entry = ManifestEntry(
            relative_path=".bashrc",
            os_profile="shared",
            added_at="2026-01-01T00:00:00+00:00",
            sensitive_flagged=False,
        )
        snap = create_snapshot([entry], home, trigger="sync", keep=5)

        # Simulate post-sync modification
        bashrc.write_text("after sync (bad)\n", encoding="utf-8")

        # Simulate a failed health check result
        check = HealthCheck(name="test_check", command="false")
        failed_result = HealthCheckResult(
            check=check, passed=False, exit_code=1, stdout="", stderr="fail", duration_ms=5,
        )

        with pytest.raises(HealthCheckFailedError):
            check_and_rollback_if_needed([failed_result], snap.id, home)

        # File should be restored to pre-sync state
        assert bashrc.read_text(encoding="utf-8") == "before sync\n"
