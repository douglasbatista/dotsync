"""Tests for dotsync.snapshot module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dotsync.git_ops import ManifestEntry
from dotsync.snapshot import (
    INDEX_FILENAME,
    SnapshotMeta,
    SnapshotNotFoundError,
    apply_retention,
    create_snapshot,
    load_index,
    rollback,
    rollback_latest,
    save_index,
    snapshot_dir_for,
    verify_snapshot,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(
    relative_path: str = ".bashrc",
    os_profile: str = "shared",
    sensitive_flagged: bool = False,
) -> ManifestEntry:
    return ManifestEntry(
        relative_path=relative_path,
        os_profile=os_profile,
        added_at="2026-01-01T00:00:00+00:00",
        sensitive_flagged=sensitive_flagged,
    )


def _make_snapshot_dir(
    snapshots_dir: Path,
    snapshot_id: str,
    files: dict[str, str] | None = None,
) -> Path:
    """Create a snapshot directory with optional files."""
    snap_dir = snapshots_dir / snapshot_id
    snap_dir.mkdir(parents=True, exist_ok=True)
    if files:
        for rel_path, content in files.items():
            f = snap_dir / rel_path
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content, encoding="utf-8")
    return snap_dir


def _write_index(snapshots_dir: Path, metas: list[SnapshotMeta]) -> None:
    """Write a snapshot index file manually."""
    from dataclasses import asdict

    index_path = snapshots_dir / INDEX_FILENAME
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    data = [asdict(m) for m in metas]
    index_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _meta(
    snapshot_id: str = "2026-01-01T00-00-00",
    created_at: str = "2026-01-01T00:00:00+00:00",
    trigger: str = "sync",
    file_count: int = 1,
    hostname: str = "testhost",
) -> SnapshotMeta:
    return SnapshotMeta(
        id=snapshot_id,
        created_at=created_at,
        trigger=trigger,
        file_count=file_count,
        hostname=hostname,
    )


@pytest.fixture(autouse=True)
def _patch_snapshots_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect SNAPSHOTS_DIR to tmp_path/snapshots for all tests."""
    snapshots = tmp_path / "snapshots"
    monkeypatch.setattr("dotsync.snapshot.SNAPSHOTS_DIR", snapshots)
    return snapshots


# ===========================================================================
# TestIndexManagement (Step 6.1)
# ===========================================================================


class TestIndexManagement:
    def test_load_index_returns_empty_list_when_no_file(self) -> None:
        result = load_index()
        assert result == []

    def test_save_and_load_index_roundtrip(self, tmp_path: Path) -> None:
        meta = _meta()
        save_index([meta])
        loaded = load_index()
        assert len(loaded) == 1
        assert loaded[0].id == meta.id
        assert loaded[0].created_at == meta.created_at
        assert loaded[0].trigger == meta.trigger
        assert loaded[0].file_count == meta.file_count
        assert loaded[0].hostname == meta.hostname

    def test_snapshot_dir_for_returns_correct_path(
        self, tmp_path: Path, _patch_snapshots_dir: Path
    ) -> None:
        result = snapshot_dir_for("2026-01-01T00-00-00")
        assert result == _patch_snapshots_dir / "2026-01-01T00-00-00"


# ===========================================================================
# TestCreateSnapshot (Step 6.2)
# ===========================================================================


class TestCreateSnapshot:
    def test_create_snapshot_copies_existing_files(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        (home / ".bashrc").write_text("# bashrc", encoding="utf-8")

        entries = [_entry(".bashrc")]
        meta = create_snapshot(entries, home, "sync", keep=0)

        snap_dir = snapshot_dir_for(meta.id)
        assert (snap_dir / ".bashrc").exists()
        assert (snap_dir / ".bashrc").read_text(encoding="utf-8") == "# bashrc"

    def test_create_snapshot_skips_missing_files(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()

        entries = [_entry(".bashrc"), _entry(".vimrc")]
        meta = create_snapshot(entries, home, "sync", keep=0)
        assert meta.file_count == 0

    def test_create_snapshot_adds_to_index(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        (home / ".bashrc").write_text("# bashrc", encoding="utf-8")

        create_snapshot([_entry(".bashrc")], home, "sync", keep=0)
        index = load_index()
        assert len(index) == 1

    def test_create_snapshot_returns_correct_meta(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        (home / ".bashrc").write_text("# bashrc", encoding="utf-8")
        (home / ".vimrc").write_text("\" vimrc", encoding="utf-8")

        entries = [_entry(".bashrc"), _entry(".vimrc")]
        meta = create_snapshot(entries, home, "restore", keep=0)

        assert meta.trigger == "restore"
        assert meta.file_count == 2
        assert meta.hostname  # non-empty


# ===========================================================================
# TestRollback (Step 6.3)
# ===========================================================================


class TestRollback:
    def test_rollback_restores_files_to_home(
        self, tmp_path: Path, _patch_snapshots_dir: Path
    ) -> None:
        snap_id = "2026-01-01T00-00-00"
        _make_snapshot_dir(
            _patch_snapshots_dir, snap_id, {".bashrc": "# original"}
        )
        _write_index(_patch_snapshots_dir, [_meta(snap_id)])

        home = tmp_path / "home"
        home.mkdir()

        restored = rollback(snap_id, home)
        assert (home / ".bashrc").read_text(encoding="utf-8") == "# original"
        assert len(restored) == 1

    def test_rollback_creates_missing_parent_dirs(
        self, tmp_path: Path, _patch_snapshots_dir: Path
    ) -> None:
        snap_id = "2026-01-01T00-00-00"
        _make_snapshot_dir(
            _patch_snapshots_dir,
            snap_id,
            {".config/nvim/init.vim": "set number"},
        )
        _write_index(_patch_snapshots_dir, [_meta(snap_id)])

        home = tmp_path / "home"
        home.mkdir()

        restored = rollback(snap_id, home)
        assert (home / ".config" / "nvim" / "init.vim").exists()
        assert len(restored) == 1

    def test_rollback_dry_run_no_writes(
        self, tmp_path: Path, _patch_snapshots_dir: Path
    ) -> None:
        snap_id = "2026-01-01T00-00-00"
        _make_snapshot_dir(
            _patch_snapshots_dir, snap_id, {".bashrc": "# original"}
        )
        _write_index(_patch_snapshots_dir, [_meta(snap_id)])

        home = tmp_path / "home"
        home.mkdir()

        restored = rollback(snap_id, home, dry_run=True)
        assert len(restored) == 1
        assert not (home / ".bashrc").exists()

    def test_rollback_raises_on_missing_snapshot(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()

        with pytest.raises(SnapshotNotFoundError, match="nonexistent"):
            rollback("nonexistent", home)

    def test_rollback_latest_uses_newest_snapshot(
        self, tmp_path: Path, _patch_snapshots_dir: Path
    ) -> None:
        old_id = "2026-01-01T00-00-00"
        new_id = "2026-02-01T00-00-00"

        _make_snapshot_dir(
            _patch_snapshots_dir, old_id, {".bashrc": "# old"}
        )
        _make_snapshot_dir(
            _patch_snapshots_dir, new_id, {".bashrc": "# new"}
        )
        _write_index(
            _patch_snapshots_dir,
            [
                _meta(old_id, created_at="2026-01-01T00:00:00+00:00"),
                _meta(new_id, created_at="2026-02-01T00:00:00+00:00"),
            ],
        )

        home = tmp_path / "home"
        home.mkdir()

        rollback_latest(home)
        assert (home / ".bashrc").read_text(encoding="utf-8") == "# new"


# ===========================================================================
# TestRetention (Step 6.4)
# ===========================================================================


class TestRetention:
    def _setup_snapshots(
        self, snapshots_dir: Path, count: int
    ) -> list[SnapshotMeta]:
        """Create N snapshot dirs and index entries."""
        metas = []
        for i in range(count):
            sid = f"2026-01-{i + 1:02d}T00-00-00"
            cat = f"2026-01-{i + 1:02d}T00:00:00+00:00"
            _make_snapshot_dir(snapshots_dir, sid, {".bashrc": f"# v{i}"})
            metas.append(_meta(sid, created_at=cat))
        _write_index(snapshots_dir, metas)
        return metas

    def test_retention_deletes_oldest_when_over_limit(
        self, _patch_snapshots_dir: Path
    ) -> None:
        self._setup_snapshots(_patch_snapshots_dir, 7)
        deleted = apply_retention(5)
        assert len(deleted) == 2
        # Oldest two should be deleted
        assert "2026-01-01T00-00-00" in deleted
        assert "2026-01-02T00-00-00" in deleted

    def test_retention_keeps_exactly_n(
        self, _patch_snapshots_dir: Path
    ) -> None:
        self._setup_snapshots(_patch_snapshots_dir, 7)
        apply_retention(5)
        remaining = load_index()
        assert len(remaining) == 5

    def test_retention_no_op_when_under_limit(
        self, _patch_snapshots_dir: Path
    ) -> None:
        self._setup_snapshots(_patch_snapshots_dir, 3)
        deleted = apply_retention(5)
        assert deleted == []
        assert len(load_index()) == 3

    def test_retention_zero_keeps_all(
        self, _patch_snapshots_dir: Path
    ) -> None:
        self._setup_snapshots(_patch_snapshots_dir, 10)
        deleted = apply_retention(0)
        assert deleted == []
        assert len(load_index()) == 10

    def test_retention_updates_index(
        self, _patch_snapshots_dir: Path
    ) -> None:
        self._setup_snapshots(_patch_snapshots_dir, 4)
        apply_retention(2)
        index = load_index()
        assert len(index) == 2
        # Newest two kept
        ids = {e.id for e in index}
        assert "2026-01-04T00-00-00" in ids
        assert "2026-01-03T00-00-00" in ids


# ===========================================================================
# TestVerifySnapshot (Step 6.5)
# ===========================================================================


class TestVerifySnapshot:
    def test_verify_complete_snapshot(
        self, _patch_snapshots_dir: Path
    ) -> None:
        snap_id = "2026-01-01T00-00-00"
        _make_snapshot_dir(
            _patch_snapshots_dir,
            snap_id,
            {".bashrc": "# bash", ".vimrc": "\" vim"},
        )

        entries = [_entry(".bashrc"), _entry(".vimrc")]
        result = verify_snapshot(snap_id, entries)
        assert result["complete"] is True
        assert result["missing"] == []
        assert result["extra"] == []

    def test_verify_detects_missing_files(
        self, _patch_snapshots_dir: Path
    ) -> None:
        snap_id = "2026-01-01T00-00-00"
        _make_snapshot_dir(
            _patch_snapshots_dir, snap_id, {".bashrc": "# bash"}
        )

        entries = [_entry(".bashrc"), _entry(".vimrc")]
        result = verify_snapshot(snap_id, entries)
        assert result["complete"] is False
        assert ".vimrc" in result["missing"]
        assert result["extra"] == []

    def test_verify_detects_extra_files(
        self, _patch_snapshots_dir: Path
    ) -> None:
        snap_id = "2026-01-01T00-00-00"
        _make_snapshot_dir(
            _patch_snapshots_dir,
            snap_id,
            {".bashrc": "# bash", ".extra": "extra"},
        )

        entries = [_entry(".bashrc")]
        result = verify_snapshot(snap_id, entries)
        assert result["complete"] is False
        assert result["missing"] == []
        assert ".extra" in result["extra"]
