"""Unit tests for the orchestration layer."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from dotsync.config import default_config
from dotsync.discovery import ConfigFile
from dotsync.flagging import FlagResult, SensitiveMatch
from dotsync.git_ops import ManifestEntry
from dotsync.orchestrator import (
    RestoreResult,
    SyncResult,
    _mark_sensitive,
    _manifest_to_config_files,
    _resolve_sensitive_confirmations,
    run_discover,
    run_restore,
    run_sync,
)


def _cfg_file(path: str, include: bool | None = True) -> ConfigFile:
    return ConfigFile(
        path=Path(path),
        abs_path=Path("/home/test") / path,
        size_bytes=100,
        include=include,
        reason="test",
    )


def _flag_result(
    cf: ConfigFile,
    *,
    matches: list[SensitiveMatch] | None = None,
    ai_flagged: bool = False,
    requires_confirmation: bool = False,
) -> FlagResult:
    return FlagResult(
        config_file=cf,
        matches=matches or [],
        ai_flagged=ai_flagged,
        requires_confirmation=requires_confirmation,
    )


# ---------------------------------------------------------------------------
# _resolve_sensitive_confirmations
# ---------------------------------------------------------------------------


class TestResolveSensitiveConfirmations:
    """Tests for _resolve_sensitive_confirmations()."""

    def test_include_clears_requires_confirmation(self) -> None:
        """Callback returning 'I' should clear requires_confirmation."""
        fr = _flag_result(_cfg_file(".bashrc"), requires_confirmation=True)
        _resolve_sensitive_confirmations([fr], lambda _fr: "I")
        assert fr.requires_confirmation is False
        assert fr.config_file.include is True

    def test_exclude_sets_include_false(self) -> None:
        """Callback returning 'E' should set include=False and clear confirmation."""
        fr = _flag_result(_cfg_file(".env"), requires_confirmation=True)
        _resolve_sensitive_confirmations([fr], lambda _fr: "E")
        assert fr.requires_confirmation is False
        assert fr.config_file.include is False

    def test_skip_leaves_untouched(self) -> None:
        """Callback returning 'S' should leave requires_confirmation as True."""
        fr = _flag_result(_cfg_file(".bashrc"), requires_confirmation=True)
        _resolve_sensitive_confirmations([fr], lambda _fr: "S")
        assert fr.requires_confirmation is True
        assert fr.config_file.include is True

    def test_default_skip_when_no_callback(self) -> None:
        """When no callback provided, defaults to skip ('S')."""
        fr = _flag_result(_cfg_file(".bashrc"), requires_confirmation=True)
        _resolve_sensitive_confirmations([fr], None)
        assert fr.requires_confirmation is True

    def test_not_requires_confirmation_is_ignored(self) -> None:
        """FlagResult not requiring confirmation is not passed to callback."""
        called: list[FlagResult] = []
        fr = _flag_result(_cfg_file(".bashrc"), requires_confirmation=False)
        _resolve_sensitive_confirmations([fr], lambda _fr: called.append(_fr) or "I")
        assert not called
        assert fr.config_file.include is True


# ---------------------------------------------------------------------------
# _mark_sensitive
# ---------------------------------------------------------------------------


class TestMarkSensitive:
    """Tests for _mark_sensitive()."""

    def test_sets_flag_on_included_match(self) -> None:
        """File with matches and confirmed (requires_confirmation=False) should be marked sensitive."""
        cf = _cfg_file(".env")
        fr = _flag_result(cf, matches=[SensitiveMatch("generic_token", 1, "to***en")])
        _mark_sensitive([fr])
        assert cf.sensitive is True

    def test_sets_flag_on_ai_flagged(self) -> None:
        """AI-flagged file that was confirmed should be marked sensitive."""
        cf = _cfg_file(".secrets")
        fr = _flag_result(cf, ai_flagged=True)
        _mark_sensitive([fr])
        assert cf.sensitive is True

    def test_skips_unconfirmed(self) -> None:
        """File still requiring confirmation should not be marked sensitive."""
        cf = _cfg_file(".bashrc")
        fr = _flag_result(
            cf,
            matches=[SensitiveMatch("generic_token", 1, "to***en")],
            requires_confirmation=True,
        )
        _mark_sensitive([fr])
        assert cf.sensitive is False

    def test_skips_clean_files(self) -> None:
        """File with no matches and not AI-flagged should stay sensitive=False."""
        cf = _cfg_file(".bashrc")
        fr = _flag_result(cf)
        _mark_sensitive([fr])
        assert cf.sensitive is False


# ---------------------------------------------------------------------------
# _manifest_to_config_files
# ---------------------------------------------------------------------------


class TestManifestToConfigFiles:
    """Tests for _manifest_to_config_files()."""

    def test_converts_manifest_entries(self) -> None:
        """ManifestEntry objects should become ConfigFile objects."""
        home = Path("/home/test")
        entries = [
            ManifestEntry(
                relative_path=".bashrc",
                os_profile="shared",
                added_at="2026-01-01",
                sensitive_flagged=False,
            ),
        ]
        result = _manifest_to_config_files(entries, home)
        assert len(result) == 1
        assert result[0].path == Path(".bashrc")
        assert result[0].include is True
        assert result[0].sensitive is False
        assert result[0].os_profile == "shared"


# ---------------------------------------------------------------------------
# run_discover
# ---------------------------------------------------------------------------


class TestRunDiscover:
    """Tests for run_discover()."""

    @pytest.fixture()
    def cfg(self) -> MagicMock:
        return default_config()

    def test_empty_scan_returns_zero_counts(
        self, cfg: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When scan returns no files, all counts should be zero."""
        monkeypatch.setattr("dotsync.orchestrator.discover", lambda *args, **kwargs: [])
        monkeypatch.setattr("dotsync.orchestrator.init_repo", lambda c: None)
        monkeypatch.setattr("dotsync.orchestrator.load_manifest", lambda p: [])

        result = run_discover(cfg, progress=None)
        assert result.registered_count == 0
        assert result.already_tracked_count == 0
        assert result.excluded_count == 0

    def test_pending_without_callback_defaults_excluded(
        self, cfg: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pending files without resolve_pending callback are excluded."""
        files = [_cfg_file(".bashrc", include=None)]
        monkeypatch.setattr(
            "dotsync.orchestrator.discover", lambda *args, **kwargs: files
        )
        monkeypatch.setattr("dotsync.orchestrator.init_repo", lambda c: None)
        monkeypatch.setattr("dotsync.orchestrator.load_manifest", lambda p: [])

        result = run_discover(cfg, progress=None)
        assert result.excluded_count == 1
        assert result.registered_count == 0

    def test_resolve_pending_callback(
        self, cfg: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resolve_pending callback can mutate include values in place."""
        files = [_cfg_file(".bashrc", include=None)]

        def _resolve(pending: list[ConfigFile]) -> None:
            for f in pending:
                f.include = True
                f.reason = "callback_confirmed"

        monkeypatch.setattr(
            "dotsync.orchestrator.discover", lambda *args, **kwargs: files
        )
        monkeypatch.setattr("dotsync.orchestrator.init_repo", lambda c: None)
        monkeypatch.setattr("dotsync.orchestrator.load_manifest", lambda p: [])
        monkeypatch.setattr(
            "dotsync.orchestrator.register_new_files", lambda *a, **kw: []
        )

        result = run_discover(
            cfg,
            progress=None,
            resolve_pending=_resolve,
            confirm_register=lambda n: True,
        )
        assert result.registered_count == 1
        assert result.excluded_count == 0

    def test_never_include_blocks_ssh_keys(
        self, cfg: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """.ssh/id_rsa should be excluded by enforce_never_include before callbacks."""
        files = [_cfg_file(".ssh/id_rsa")]
        monkeypatch.setattr(
            "dotsync.orchestrator.discover", lambda *args, **kwargs: files
        )
        monkeypatch.setattr("dotsync.orchestrator.init_repo", lambda c: None)
        monkeypatch.setattr("dotsync.orchestrator.load_manifest", lambda p: [])

        result = run_discover(cfg, progress=None)
        assert files[0].include is False
        assert files[0].reason == "never_include"
        assert result.excluded_count == 1

    def test_registration_calls_register_new_files(
        self, cfg: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """confirm_register → True should trigger registration via register_new_files."""
        files = [_cfg_file(".bashrc", include=True)]
        calls: list[object] = []

        def fake_register(
            new_files, repo_path, home, cfg_obj, dry_run=False
        ):
            calls.append((new_files,))
            return []

        monkeypatch.setattr(
            "dotsync.orchestrator.discover", lambda *args, **kwargs: files
        )
        monkeypatch.setattr("dotsync.orchestrator.init_repo", lambda c: None)
        monkeypatch.setattr("dotsync.orchestrator.load_manifest", lambda p: [])
        monkeypatch.setattr("dotsync.orchestrator.register_new_files", fake_register)

        result = run_discover(
            cfg,
            progress=None,
            confirm_register=lambda n: True,
        )
        assert result.registered_count == 1
        assert len(calls) == 1

    def test_registration_skipped_when_callback_returns_false(
        self, cfg: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """confirm_register → False should skip registration."""
        files = [_cfg_file(".bashrc", include=True)]

        monkeypatch.setattr(
            "dotsync.orchestrator.discover", lambda *args, **kwargs: files
        )
        monkeypatch.setattr("dotsync.orchestrator.init_repo", lambda c: None)
        monkeypatch.setattr("dotsync.orchestrator.load_manifest", lambda p: [])
        monkeypatch.setattr(
            "dotsync.orchestrator.register_new_files", lambda *a, **k: []
        )

        result = run_discover(
            cfg,
            progress=None,
            confirm_register=lambda n: False,
        )
        assert result.registered_count == 0

    def test_tracked_files_not_registered(
        self, cfg: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Files already in manifest should not be re-registered."""
        files = [_cfg_file(".bashrc", include=True)]
        manifest = [
            ManifestEntry(
                relative_path=".bashrc",
                os_profile="shared",
                added_at="2026-01-01",
                sensitive_flagged=False,
            )
        ]

        monkeypatch.setattr(
            "dotsync.orchestrator.discover", lambda *args, **kwargs: files
        )
        monkeypatch.setattr("dotsync.orchestrator.init_repo", lambda c: None)
        monkeypatch.setattr("dotsync.orchestrator.load_manifest", lambda p: manifest)
        monkeypatch.setattr(
            "dotsync.orchestrator.register_new_files", lambda *a, **k: []
        )

        result = run_discover(cfg, progress=None, confirm_register=lambda n: True)
        assert result.registered_count == 0
        assert result.already_tracked_count == 1
        assert result.excluded_count == 0

    def test_progress_callback_passed_to_discover(
        self, cfg: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The progress callback should be forwarded to discover()."""
        passed_progress = None

        def fake_discover(c, progress=None):
            nonlocal passed_progress
            passed_progress = progress
            return []

        monkeypatch.setattr("dotsync.orchestrator.discover", fake_discover)
        monkeypatch.setattr("dotsync.orchestrator.init_repo", lambda c: None)
        monkeypatch.setattr("dotsync.orchestrator.load_manifest", lambda p: [])

        def my_progress(event: object) -> None:
            pass

        run_discover(cfg, progress=my_progress)
        assert passed_progress is my_progress




# ---------------------------------------------------------------------------
# run_sync
# ---------------------------------------------------------------------------


class TestRunSync:
    """Tests for run_sync()."""

    @pytest.fixture()
    def cfg(self) -> MagicMock:
        return default_config()

    def test_dry_run_does_not_copy(
        self, cfg: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dry run should not execute file copies or commits."""
        manifest = [
            ManifestEntry(
                relative_path=".bashrc",
                os_profile="shared",
                added_at="2026-01-01",
                sensitive_flagged=False,
            ),
        ]

        monkeypatch.setattr("dotsync.orchestrator.load_manifest", lambda p: manifest)
        monkeypatch.setattr(
            "dotsync.orchestrator.flag_all", lambda f, c, progress=None: []
        )
        monkeypatch.setattr("dotsync.orchestrator.init_repo", lambda c: MagicMock())
        monkeypatch.setattr(
            "dotsync.orchestrator.create_snapshot",
            lambda *args, **kwargs: MagicMock(id="snap", file_count=1),
        )
        monkeypatch.setattr(
            "dotsync.orchestrator.plan_sync", lambda *args, **kwargs: []
        )

        result = run_sync(cfg, dry_run=True)
        assert isinstance(result, SyncResult)
        assert result.copied_count == 0
        assert result.committed is False
        assert result.pushed is False

    def test_sync_creates_snapshot(
        self, cfg: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sync should create a snapshot before executing."""
        manifest = [
            ManifestEntry(
                relative_path=".bashrc",
                os_profile="shared",
                added_at="2026-01-01",
                sensitive_flagged=False,
            ),
        ]
        snap_called: list[tuple] = []

        def fake_snapshot(manifest, home, trigger, keep):
            snap_called.append((trigger, keep))
            return MagicMock(id="snap", file_count=1)

        monkeypatch.setattr("dotsync.orchestrator.load_manifest", lambda p: manifest)
        monkeypatch.setattr(
            "dotsync.orchestrator.flag_all", lambda f, c, progress=None: []
        )
        monkeypatch.setattr("dotsync.orchestrator.init_repo", lambda c: MagicMock())
        monkeypatch.setattr("dotsync.orchestrator.create_snapshot", fake_snapshot)
        monkeypatch.setattr("dotsync.orchestrator.plan_sync", lambda *a, **k: [])

        result = run_sync(cfg, dry_run=True)
        assert snap_called == [("sync", cfg.snapshot_keep)]
        assert result.snapshot.id == "snap"

    def test_sync_executes_and_commits(
        self, cfg: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With confirm_execute=True, sync should execute, commit, and push."""
        from dotsync.sync import SyncAction as _SyncAction

        manifest = [
            ManifestEntry(
                relative_path=".bashrc",
                os_profile="shared",
                added_at="2026-01-01",
                sensitive_flagged=False,
            ),
        ]
        action = _SyncAction(
            source=Path("/home/test/.bashrc"),
            destination=Path("/repo/.bashrc"),
            action="copy",
            transformed=False,
        )
        executed = []

        def fake_execute(actions, dry_run=False):
            executed.extend(actions)
            return actions

        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = True
        mock_repo.untracked_files = []

        monkeypatch.setattr("dotsync.orchestrator.load_manifest", lambda p: manifest)
        monkeypatch.setattr(
            "dotsync.orchestrator.flag_all", lambda f, c, progress=None: []
        )
        monkeypatch.setattr("dotsync.orchestrator.init_repo", lambda c: mock_repo)
        monkeypatch.setattr(
            "dotsync.orchestrator.create_snapshot",
            lambda *a, **k: MagicMock(id="snap", file_count=1),
        )
        monkeypatch.setattr("dotsync.orchestrator.plan_sync", lambda *a, **k: [action])
        monkeypatch.setattr("dotsync.orchestrator.execute_sync", fake_execute)
        monkeypatch.setattr(
            "dotsync.orchestrator.commit_and_push", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "dotsync.orchestrator.post_operation_checks", lambda *a, **k: None
        )

        result = run_sync(cfg, confirm_execute=lambda copied, skipped: True)
        assert result.copied_count == 1
        assert result.committed is True
        assert result.pushed is True
        assert len(executed) == 1

    def test_sync_no_push(
        self, cfg: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """no_push=True should commit without pushing."""
        manifest = [
            ManifestEntry(
                relative_path=".bashrc",
                os_profile="shared",
                added_at="2026-01-01",
                sensitive_flagged=False,
            ),
        ]
        action = MagicMock(action="copy")
        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = True
        mock_repo.untracked_files = []

        monkeypatch.setattr("dotsync.orchestrator.load_manifest", lambda p: manifest)
        monkeypatch.setattr(
            "dotsync.orchestrator.flag_all", lambda f, c, progress=None: []
        )
        monkeypatch.setattr("dotsync.orchestrator.init_repo", lambda c: mock_repo)
        monkeypatch.setattr(
            "dotsync.orchestrator.create_snapshot",
            lambda *a, **k: MagicMock(id="snap", file_count=1),
        )
        monkeypatch.setattr("dotsync.orchestrator.plan_sync", lambda *a, **k: [action])
        monkeypatch.setattr(
            "dotsync.orchestrator.execute_sync",
            lambda actions, dry_run=False: actions,
        )
        monkeypatch.setattr(
            "dotsync.orchestrator.post_operation_checks", lambda *a, **k: None
        )

        result = run_sync(
            cfg, no_push=True, confirm_execute=lambda copied, skipped: True
        )
        assert result.committed is True
        assert result.pushed is False

    def test_sync_confirm_execute_false_skips(
        self, cfg: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If confirm_execute returns False, execution should not happen."""
        manifest = [
            ManifestEntry(
                relative_path=".bashrc",
                os_profile="shared",
                added_at="2026-01-01",
                sensitive_flagged=False,
            ),
        ]
        action = MagicMock(action="copy")
        mock_repo = MagicMock()

        monkeypatch.setattr("dotsync.orchestrator.load_manifest", lambda p: manifest)
        monkeypatch.setattr(
            "dotsync.orchestrator.flag_all", lambda f, c, progress=None: []
        )
        monkeypatch.setattr("dotsync.orchestrator.init_repo", lambda c: mock_repo)
        monkeypatch.setattr(
            "dotsync.orchestrator.create_snapshot",
            lambda *a, **k: MagicMock(id="snap", file_count=1),
        )
        monkeypatch.setattr("dotsync.orchestrator.plan_sync", lambda *a, **k: [action])
        monkeypatch.setattr("dotsync.orchestrator.execute_sync", lambda *a, **k: [])
        monkeypatch.setattr(
            "dotsync.orchestrator.commit_and_push",
            MagicMock(side_effect=RuntimeError("should not be called")),
        )

        result = run_sync(cfg, confirm_execute=lambda copied, skipped: False)
        assert result.committed is False
        assert result.pushed is False

    def test_sync_health_failure_raises(
        self, cfg: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Health check failure should raise HealthCheckFailedError."""
        from dotsync.health import HealthCheckFailedError

        manifest = [
            ManifestEntry(
                relative_path=".bashrc",
                os_profile="shared",
                added_at="2026-01-01",
                sensitive_flagged=False,
            ),
        ]

        monkeypatch.setattr("dotsync.orchestrator.load_manifest", lambda p: manifest)
        monkeypatch.setattr(
            "dotsync.orchestrator.flag_all", lambda f, c, progress=None: []
        )
        monkeypatch.setattr("dotsync.orchestrator.init_repo", lambda c: MagicMock())
        monkeypatch.setattr(
            "dotsync.orchestrator.create_snapshot",
            lambda *a, **k: MagicMock(id="snap", file_count=1),
        )

        from dotsync.sync import SyncAction as _SyncAction

        action = _SyncAction(
            source=Path("/home/test/.bashrc"),
            destination=Path("/repo/.bashrc"),
            action="copy",
            transformed=False,
        )
        monkeypatch.setattr("dotsync.orchestrator.plan_sync", lambda *a, **k: [action])
        monkeypatch.setattr(
            "dotsync.orchestrator.execute_sync", lambda actions, dry_run=False: actions
        )
        monkeypatch.setattr(
            "dotsync.orchestrator.commit_and_push", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "dotsync.orchestrator.post_operation_checks",
            MagicMock(side_effect=HealthCheckFailedError("fail")),
        )

        with pytest.raises(HealthCheckFailedError):
            run_sync(cfg, confirm_execute=lambda c, s: True)


# ---------------------------------------------------------------------------
# run_restore
# ---------------------------------------------------------------------------


class TestRunRestore:
    """Tests for run_restore()."""

    @pytest.fixture()
    def cfg(self) -> MagicMock:
        return default_config()

    def test_from_snapshot_uses_snapshot_rollback(
        self, cfg: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """from_snapshot should call snapshot_rollback directly."""
        called_with: list[str] = []

        def fake_rollback(snap_id, home, dry_run=False):
            called_with.append(snap_id)
            return [Path("/home/test/.bashrc")]

        monkeypatch.setattr("dotsync.orchestrator.snapshot_rollback", fake_rollback)

        result = run_restore(cfg, from_snapshot="snap-123", dry_run=False)
        assert called_with == ["snap-123"]
        assert isinstance(result, RestoreResult)
        assert result.restored_count == 1
        assert result.snapshot is None

    def test_normal_restore(
        self, cfg: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Normal restore should pull, snapshot, plan, execute, and check health."""
        manifest = [
            ManifestEntry(
                relative_path=".bashrc",
                os_profile="shared",
                added_at="2026-01-01",
                sensitive_flagged=False,
            ),
        ]
        mock_snap = MagicMock(id="snap-1", file_count=1)

        monkeypatch.setattr("dotsync.orchestrator.init_repo", lambda c: MagicMock())
        monkeypatch.setattr("dotsync.orchestrator.pull", lambda r: None)
        monkeypatch.setattr("dotsync.orchestrator.load_manifest", lambda p: manifest)
        monkeypatch.setattr(
            "dotsync.orchestrator.create_snapshot", lambda *a, **k: mock_snap
        )
        monkeypatch.setattr("dotsync.orchestrator.plan_restore", lambda *a, **k: [])
        monkeypatch.setattr("dotsync.orchestrator.execute_restore", lambda *a, **k: [])
        monkeypatch.setattr(
            "dotsync.orchestrator.post_operation_checks", lambda *a, **k: None
        )

        result = run_restore(cfg, dry_run=False)
        assert isinstance(result, RestoreResult)
        assert result.snapshot == mock_snap

    def test_restore_dry_run_no_writes(
        self, cfg: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dry run should not execute restore or health checks."""
        manifest = [
            ManifestEntry(
                relative_path=".bashrc",
                os_profile="shared",
                added_at="2026-01-01",
                sensitive_flagged=False,
            ),
        ]
        executed = []

        monkeypatch.setattr("dotsync.orchestrator.init_repo", lambda c: MagicMock())
        monkeypatch.setattr("dotsync.orchestrator.pull", lambda r: None)
        monkeypatch.setattr("dotsync.orchestrator.load_manifest", lambda p: manifest)
        monkeypatch.setattr(
            "dotsync.orchestrator.create_snapshot",
            lambda *a, **k: MagicMock(id="snap-1", file_count=1),
        )
        monkeypatch.setattr("dotsync.orchestrator.plan_restore", lambda *a, **k: [])
        monkeypatch.setattr(
            "dotsync.orchestrator.execute_restore",
            lambda a, dry_run=False: executed.append(dry_run) or a,
        )
        monkeypatch.setattr(
            "dotsync.orchestrator.post_operation_checks",
            MagicMock(side_effect=RuntimeError("should not be called")),
        )

        result = run_restore(cfg, dry_run=True)
        # In dry-run mode, execute_restore is never called
        assert executed == []
        assert result.skipped_count == 0

    def test_restore_no_pull(
        self, cfg: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """no_pull=True should skip the pull step."""
        pulled: list = []

        def fake_pull(repo):
            pulled.append(repo)

        monkeypatch.setattr("dotsync.orchestrator.init_repo", lambda c: MagicMock())
        monkeypatch.setattr("dotsync.orchestrator.pull", fake_pull)
        monkeypatch.setattr("dotsync.orchestrator.load_manifest", lambda p: [])
        monkeypatch.setattr(
            "dotsync.orchestrator.create_snapshot", lambda *a, **k: MagicMock()
        )
        monkeypatch.setattr("dotsync.orchestrator.plan_restore", lambda *a, **k: [])
        monkeypatch.setattr("dotsync.orchestrator.execute_restore", lambda *a, **k: [])
        monkeypatch.setattr(
            "dotsync.orchestrator.post_operation_checks", lambda *a, **k: None
        )

        run_restore(cfg, no_pull=True)
        assert not pulled

    def test_restore_no_remote_silently_continues(
        self, cfg: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NoRemoteConfiguredError during pull should not abort."""
        from dotsync.git_ops import NoRemoteConfiguredError

        def fake_pull(repo):
            raise NoRemoteConfiguredError("no remote")

        monkeypatch.setattr("dotsync.orchestrator.init_repo", lambda c: MagicMock())
        monkeypatch.setattr("dotsync.orchestrator.pull", fake_pull)
        monkeypatch.setattr("dotsync.orchestrator.load_manifest", lambda p: [])
        monkeypatch.setattr(
            "dotsync.orchestrator.create_snapshot", lambda *a, **k: MagicMock()
        )
        monkeypatch.setattr("dotsync.orchestrator.plan_restore", lambda *a, **k: [])
        monkeypatch.setattr("dotsync.orchestrator.execute_restore", lambda *a, **k: [])
        monkeypatch.setattr(
            "dotsync.orchestrator.post_operation_checks", lambda *a, **k: None
        )

        result = run_restore(cfg)
        assert isinstance(result, RestoreResult)
