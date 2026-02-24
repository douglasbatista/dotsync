"""Local snapshot management for DotSync.

Creates timestamped snapshots of managed files before write operations
(sync/restore), supports rollback to any snapshot, and enforces a
configurable retention policy.  Snapshots are entirely local — they
never enter the Git repository.
"""

from __future__ import annotations

import json
import shutil
import socket
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from dotsync.config import CONFIG_DIR
from dotsync.git_ops import ManifestEntry

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SNAPSHOTS_DIR = CONFIG_DIR / "snapshots"
INDEX_FILENAME = "snapshot_index.json"

# ---------------------------------------------------------------------------
# Data model & exceptions
# ---------------------------------------------------------------------------


@dataclass
class SnapshotMeta:
    """Metadata for a single snapshot."""

    id: str  # e.g. "2026-02-22T14-30-00"
    created_at: str  # ISO 8601: "2026-02-22T14:30:00+00:00"
    trigger: str  # "sync" or "restore"
    file_count: int
    hostname: str


class SnapshotNotFoundError(Exception):
    """Raised when a requested snapshot does not exist."""


# ---------------------------------------------------------------------------
# Index management
# ---------------------------------------------------------------------------


def snapshot_dir_for(snapshot_id: str) -> Path:
    """Return the directory path for a given snapshot ID."""
    return SNAPSHOTS_DIR / snapshot_id


def load_index() -> list[SnapshotMeta]:
    """Read the snapshot index file, returning [] if missing or corrupt."""
    index_path = SNAPSHOTS_DIR / INDEX_FILENAME
    if not index_path.exists():
        return []
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        return [SnapshotMeta(**entry) for entry in data]
    except (json.JSONDecodeError, OSError, TypeError, KeyError):
        return []


def save_index(entries: list[SnapshotMeta]) -> None:
    """Write the snapshot index to disk."""
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    index_path = SNAPSHOTS_DIR / INDEX_FILENAME
    data = [asdict(e) for e in entries]
    index_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Snapshot creation
# ---------------------------------------------------------------------------


def create_snapshot(
    entries: list[ManifestEntry],
    home: Path,
    trigger: Literal["sync", "restore"],
    keep: int = 5,
) -> SnapshotMeta:
    """Create a timestamped snapshot of managed files.

    Copies each file listed in *entries* (if it exists under *home*) into
    a new snapshot directory, preserving relative paths.  Appends metadata
    to the index and runs retention cleanup.

    Args:
        entries: Manifest entries describing files to snapshot.
        home: Home directory root.
        trigger: What caused the snapshot ("sync" or "restore").
        keep: Maximum snapshots to retain (0 = unlimited).

    Returns:
        Metadata for the newly created snapshot.
    """
    now = datetime.now(tz=timezone.utc)
    snapshot_id = now.strftime("%Y-%m-%dT%H-%M-%S")
    snap_dir = snapshot_dir_for(snapshot_id)
    snap_dir.mkdir(parents=True, exist_ok=True)

    file_count = 0
    for entry in entries:
        src = home / entry.relative_path
        if not src.exists():
            continue
        dest = snap_dir / entry.relative_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        file_count += 1

    meta = SnapshotMeta(
        id=snapshot_id,
        created_at=now.isoformat(),
        trigger=trigger,
        file_count=file_count,
        hostname=socket.gethostname(),
    )

    index = load_index()
    index.append(meta)
    save_index(index)

    apply_retention(keep)

    return meta


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def list_snapshots() -> list[SnapshotMeta]:
    """Return all snapshots sorted newest-first by created_at."""
    entries = load_index()
    entries.sort(key=lambda e: e.created_at, reverse=True)
    return entries


def rollback(
    snapshot_id: str,
    home: Path,
    dry_run: bool = False,
) -> list[Path]:
    """Restore files from a snapshot back to the home directory.

    Args:
        snapshot_id: ID of the snapshot to restore from.
        home: Home directory root where files will be written.
        dry_run: If True, return paths without writing.

    Returns:
        List of restored (or would-be-restored) destination paths.

    Raises:
        SnapshotNotFoundError: If the snapshot directory does not exist.
    """
    snap_dir = snapshot_dir_for(snapshot_id)
    if not snap_dir.exists():
        raise SnapshotNotFoundError(f"Snapshot not found: {snapshot_id}")

    restored: list[Path] = []
    for src in snap_dir.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(snap_dir)
        dest = home / rel
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        restored.append(dest)

    return restored


def rollback_latest(
    home: Path,
    dry_run: bool = False,
) -> list[Path]:
    """Restore files from the most recent snapshot.

    Args:
        home: Home directory root.
        dry_run: If True, return paths without writing.

    Returns:
        List of restored (or would-be-restored) destination paths.

    Raises:
        SnapshotNotFoundError: If no snapshots exist.
    """
    snapshots = list_snapshots()
    if not snapshots:
        raise SnapshotNotFoundError("No snapshots available")
    return rollback(snapshots[0].id, home, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Retention policy
# ---------------------------------------------------------------------------


def apply_retention(keep: int) -> list[str]:
    """Delete snapshots beyond the retention limit.

    Args:
        keep: Maximum number of snapshots to keep.  0 means keep all.

    Returns:
        List of deleted snapshot IDs.
    """
    if keep == 0:
        return []

    index = load_index()
    index.sort(key=lambda e: e.created_at, reverse=True)

    to_keep = index[:keep]
    to_delete = index[keep:]

    deleted_ids: list[str] = []
    for entry in to_delete:
        snap_dir = snapshot_dir_for(entry.id)
        if snap_dir.exists():
            shutil.rmtree(snap_dir)
        deleted_ids.append(entry.id)

    if deleted_ids:
        save_index(to_keep)

    return deleted_ids


# ---------------------------------------------------------------------------
# Snapshot integrity check
# ---------------------------------------------------------------------------


def verify_snapshot(
    snapshot_id: str,
    entries: list[ManifestEntry],
) -> dict[str, bool | list[str]]:
    """Check a snapshot's completeness against a manifest.

    Args:
        snapshot_id: ID of the snapshot to verify.
        entries: Expected manifest entries.

    Returns:
        Dict with keys ``complete`` (bool), ``missing`` (list of relative
        paths expected but absent), and ``extra`` (list of relative paths
        present but not expected).

    Raises:
        SnapshotNotFoundError: If the snapshot directory does not exist.
    """
    snap_dir = snapshot_dir_for(snapshot_id)
    if not snap_dir.exists():
        raise SnapshotNotFoundError(f"Snapshot not found: {snapshot_id}")

    expected = {e.relative_path for e in entries}
    actual = {
        str(p.relative_to(snap_dir))
        for p in snap_dir.rglob("*")
        if p.is_file()
    }

    missing = sorted(expected - actual)
    extra = sorted(actual - expected)

    return {
        "complete": len(missing) == 0 and len(extra) == 0,
        "missing": missing,
        "extra": extra,
    }
