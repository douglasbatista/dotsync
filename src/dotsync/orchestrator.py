"""Orchestration layer for DotSync workflows.

Contains pure business-logic functions (no Typer/Rich imports) that
sequence the operations for discover, sync, and restore.  Each function
returns a structured result dataclass that the CLI layer can render.

Interaction callbacks (``confirm_...``) are injected by the CLI so that
prompts and tables live in the presentation layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, cast

from dotsync.config import DotSyncConfig
from dotsync.discovery import ConfigFile, ProgressCallback, discover
from dotsync.flagging import FlagResult, enforce_never_include, flag_all
from dotsync.git_ops import (
    ManifestEntry,
    NoRemoteConfiguredError,
    commit_and_push,
    init_repo,
    load_manifest,
    pull,
)
from dotsync.health import post_operation_checks
from dotsync.platform_utils import current_os, home_dir
from dotsync.snapshot import (
    SnapshotMeta,
    create_snapshot,
    rollback as snapshot_rollback,
)
from dotsync.sync import (
    RestoreAction,
    SyncAction,
    execute_restore,
    execute_sync,
    plan_restore,
    plan_sync,
    register_new_files,
)

# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class DiscoverResult:
    """Result of a discover workflow.

    Attributes:
        files: All classified ConfigFile objects.
        registered_count: Number of new files added to the manifest.
        already_tracked_count: Number of included files already in manifest.
        excluded_count: Number of files excluded by heuristics/AI/user.
    """

    files: list[ConfigFile]
    registered_count: int
    already_tracked_count: int
    excluded_count: int


@dataclass
class SyncResult:
    """Result of a sync workflow.

    Attributes:
        manifest: Manifest entries that were synced.
        snapshot: Pre-sync snapshot metadata.
        actions: All planned sync actions.
        copied_count: Number of files actually copied.
        skipped_count: Number of files skipped.
        committed: Whether a commit was made.
        pushed: Whether changes were pushed to remote.
    """

    manifest: list[ManifestEntry]
    snapshot: SnapshotMeta
    actions: list[SyncAction]
    copied_count: int
    skipped_count: int
    committed: bool
    pushed: bool


@dataclass
class RestoreResult:
    """Result of a restore workflow.

    Attributes:
        actions: All planned restore actions.
        restored_count: Number of files restored.
        skipped_count: Number of files skipped.
        snapshot: Pre-restore snapshot metadata (None for snapshot rollback).
    """

    actions: list[RestoreAction]
    restored_count: int
    skipped_count: int
    snapshot: SnapshotMeta | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _manifest_to_config_files(
    entries: list[ManifestEntry], home: Path
) -> list[ConfigFile]:
    """Convert ManifestEntry objects to ConfigFile objects for flagging.

    Args:
        entries: ManifestEntry objects from the manifest.
        home: Home directory path.

    Returns:
        List of ConfigFile objects.
    """
    result: list[ConfigFile] = []
    for e in entries:
        abs_path = home / e.relative_path
        size = abs_path.stat().st_size if abs_path.exists() else 0
        result.append(
            ConfigFile(
                path=Path(e.relative_path),
                abs_path=abs_path,
                size_bytes=size,
                include=True,
                sensitive=e.sensitive_flagged,
                reason="manifest",
                os_profile=cast(Literal["linux", "windows", "shared"], e.os_profile),
            )
        )
    return result


def _resolve_sensitive_confirmations(
    flag_results: list[FlagResult],
    resolve: Callable[[FlagResult], str] | None,
) -> None:
    """Resolve sensitive file confirmations using the provided callback.

    If no callback is provided, defaults to ``"S"`` (skip) for all flagged files.

    Args:
        flag_results: FlagResult objects from flag_all().
        resolve: Callback receiving a FlagResult and returning
            ``"I"`` (Include), ``"E"`` (Exclude), or ``"S"`` (Skip).
    """
    for fr in flag_results:
        if not fr.requires_confirmation:
            continue

        choice = resolve(fr).strip().upper() if resolve is not None else "S"

        if choice == "I":
            fr.requires_confirmation = False
        elif choice == "E":
            fr.config_file.include = False
            fr.requires_confirmation = False
        # "S" or anything else → leave as-is


def _mark_sensitive(flag_results: list[FlagResult]) -> None:
    """Mark ConfigFiles as sensitive based on resolved flagging results.

    Any file that had detections (regex matches or AI-flagged) and was
    confirmed for inclusion (``requires_confirmation is False``) is marked
    ``sensitive=True``.

    Args:
        flag_results: FlagResult objects (already confirmed).
    """
    for fr in flag_results:
        if (fr.matches or fr.ai_flagged) and not fr.requires_confirmation:
            fr.config_file.sensitive = True


# ---------------------------------------------------------------------------
# Discover orchestrator
# ---------------------------------------------------------------------------


def run_discover(
    cfg: DotSyncConfig,
    *,
    resolve_pending: Callable[[list[ConfigFile]], None] | None = None,
    confirm_register: Callable[[int], bool] | None = None,
    progress: ProgressCallback | None = None,
) -> DiscoverResult:
    """Run the full discover workflow.

    Scans for config files, classifies them, enforces the NEVER_INCLUDE
    blocklist, resolves pending classifications, and registers new files
    into the manifest.

    Sensitive data flagging is performed at sync time, not during
    discovery — see :func:`run_sync`.

    Args:
        cfg: DotSync configuration.
        resolve_pending: Optional callback receiving all pending ConfigFile
            objects.  The callback should mutate ``include`` / ``reason`` in
            place.  When ``None``, pending files are treated as excluded.
        confirm_register: Optional callback receiving the number of files
            to register. Return ``True`` to proceed. When ``None``,
            registration is skipped.
        progress: Optional scan progress callback for real-time events.

    Returns:
        A :class:`DiscoverResult` with full results for the CLI to render.
    """
    home = home_dir()
    init_repo(cfg)

    # 1. Scan & classify
    files = discover(cfg, progress=progress)

    # 2. Enforce NEVER_INCLUDE blocklist immediately after classification
    enforce_never_include(files)

    # 3. Resolve pending files via callback
    pending = [f for f in files if f.include is None]
    if pending:
        if resolve_pending is not None:
            resolve_pending(pending)
        else:
            for f in pending:
                f.include = False
                f.reason = "user_excluded"

    # 4. Load manifest to identify already-tracked files
    manifest = load_manifest(cfg.repo_path)
    tracked_paths = {e.relative_path for e in manifest}

    to_register = [
        f for f in files if f.include is True and str(f.path) not in tracked_paths
    ]
    already_tracked = [
        f for f in files if f.include is True and str(f.path) in tracked_paths
    ]
    excluded_final = [f for f in files if f.include is not True]

    # 5. Register new files
    registered_count = 0
    if to_register:
        if confirm_register is not None and confirm_register(len(to_register)):
            register_new_files(to_register, cfg.repo_path, home, cfg)
            registered_count = len(to_register)

    return DiscoverResult(
        files=files,
        registered_count=registered_count,
        already_tracked_count=len(already_tracked),
        excluded_count=len(excluded_final),
    )


# ---------------------------------------------------------------------------
# Sync orchestrator
# ---------------------------------------------------------------------------


def run_sync(
    cfg: DotSyncConfig,
    *,
    dry_run: bool = False,
    no_push: bool = False,
    message: str = "dotsync: sync",
    resolve_sensitive: Callable[[FlagResult], str] | None = None,
    confirm_execute: Callable[[int, int], bool] | None = None,
) -> SyncResult:
    """Run the full sync workflow.

    Loads the manifest, flags sensitive data, creates a pre-sync snapshot,
    plans and executes the sync, commits changes, and runs health checks.

    Args:
        cfg: DotSync configuration.
        dry_run: If ``True``, skip all filesystem writes.
        no_push: If ``True``, commit but do not push.
        message: Commit message.
        resolve_sensitive: Optional callback for sensitive file confirmation.
            Defaults to ``"S"`` (skip).
        confirm_execute: Optional callback receiving ``(copied_count,
            skipped_count)``. Return ``True`` to proceed with execution.
            When ``None``, execution proceeds unconditionally.

    Returns:
        A :class:`SyncResult` with full results for the CLI to render.

    Raises:
        HealthCheckFailedError: If health checks fail after the sync.
    """
    home = home_dir()
    os_name = current_os()

    # 1. Load manifest
    manifest = load_manifest(cfg.repo_path)

    # 2. Flag manifest entries for sensitive data
    config_files = _manifest_to_config_files(manifest, home)
    flag_results = flag_all(config_files, cfg)

    _resolve_sensitive_confirmations(flag_results, resolve_sensitive)
    _mark_sensitive(flag_results)

    # 3. Snapshot before sync
    repo = init_repo(cfg)
    snap = create_snapshot(manifest, home, trigger="sync", keep=cfg.snapshot_keep)

    # 4. Plan sync
    actions = plan_sync(manifest, home, cfg.repo_path, os_name)
    copied = [a for a in actions if a.action == "copy"]
    skipped = [a for a in actions if a.action != "copy"]

    # 5. Execute sync (iff user confirms and not dry-run)
    should_execute = copied and not dry_run
    if should_execute and confirm_execute is not None:
        should_execute = confirm_execute(len(copied), len(skipped))

    if not dry_run and should_execute:
        execute_sync(actions, dry_run=False)

    # 6. Commit & push
    committed = False
    pushed = False
    if not dry_run and should_execute:
        try:
            if no_push:
                repo.git.add(A=True)
                if repo.is_dirty(index=True) or repo.untracked_files:
                    repo.index.commit(message)
                    committed = True
            else:
                commit_and_push(repo, message)
                committed = True
                pushed = True
        except NoRemoteConfiguredError:
            repo.git.add(A=True)
            if repo.is_dirty(index=True) or repo.untracked_files:
                repo.index.commit(message)
                committed = True
        except Exception:
            # Push failed — commit may already be local
            committed = True

    # 7. Health checks
    if not dry_run and should_execute:
        post_operation_checks(cfg, snap.id, home, operation="sync")

    return SyncResult(
        manifest=manifest,
        snapshot=snap,
        actions=actions,
        copied_count=len(copied),
        skipped_count=len(skipped),
        committed=committed,
        pushed=pushed,
    )


# ---------------------------------------------------------------------------
# Restore orchestrator
# ---------------------------------------------------------------------------


def run_restore(
    cfg: DotSyncConfig,
    *,
    dry_run: bool = False,
    no_pull: bool = False,
    from_snapshot: str | None = None,
) -> RestoreResult:
    """Run the full restore workflow.

    Pulls latest changes, creates a pre-restore snapshot, plans and
    executes the restore, and runs health checks.

    Args:
        cfg: DotSync configuration.
        dry_run: If ``True``, skip all filesystem writes.
        no_pull: If ``True``, skip pulling from remote.
        from_snapshot: If set, restore directly from this snapshot ID
            instead of the repository.

    Returns:
        A :class:`RestoreResult` with full results for the CLI to render.

    Raises:
        SnapshotNotFoundError: If the specified snapshot does not exist.
        HealthCheckFailedError: If health checks fail after the restore.
    """
    home = home_dir()
    os_name = current_os()

    # Shortcut: restore from snapshot directly
    if from_snapshot:
        restored = snapshot_rollback(from_snapshot, home, dry_run=dry_run)
        return RestoreResult(
            actions=[],  # snapshot rollback doesn't produce RestoreAction objects
            restored_count=len(restored),
            skipped_count=0,
            snapshot=None,
        )

    repo = init_repo(cfg)

    # 1. Pull
    if not no_pull:
        try:
            pull(repo)
        except NoRemoteConfiguredError:
            pass  # silently skip

    # 2. Load manifest & snapshot
    manifest = load_manifest(cfg.repo_path)
    snap = create_snapshot(manifest, home, trigger="restore", keep=cfg.snapshot_keep)

    # 3. Plan & execute restore
    actions = plan_restore(manifest, home, cfg.repo_path, os_name)
    if not dry_run:
        execute_restore(actions, dry_run=False)

    restored_actions = [a for a in actions if a.action == "restore"]
    skipped_actions = [a for a in actions if a.action != "restore"]

    # 4. Health checks
    if not dry_run:
        post_operation_checks(cfg, snap.id, home, operation="restore")

    return RestoreResult(
        actions=actions,
        restored_count=len(restored_actions),
        skipped_count=len(skipped_actions),
        snapshot=snap,
    )
