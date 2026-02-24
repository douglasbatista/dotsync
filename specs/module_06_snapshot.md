# Module 06 — Snapshot & Rollback

## Overview

Local-only timestamped snapshots of managed files, created before any write operation (sync/restore). Provides rollback to any previous snapshot and a configurable retention policy. Snapshots never enter the Git repository.

## Dependencies

- `config.CONFIG_DIR` → `Path.home() / ".dotsync"` — parent for snapshots directory
- `config.DotSyncConfig.snapshot_keep` → `int`, default 5 — retention limit (passed by CLI layer)
- `git_ops.ManifestEntry` — `relative_path`, `os_profile`, `added_at`, `sensitive_flagged`

## Constants

- `SNAPSHOTS_DIR = CONFIG_DIR / "snapshots"` — `~/.dotsync/snapshots/`
- `INDEX_FILENAME = "snapshot_index.json"` — metadata index file within `SNAPSHOTS_DIR`

## Data Model

### `SnapshotMeta` (dataclass)

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Timestamp ID, e.g. `"2026-02-22T14-30-00"` |
| `created_at` | `str` | ISO 8601 datetime, e.g. `"2026-02-22T14:30:00+00:00"` |
| `trigger` | `str` | `"sync"` or `"restore"` |
| `file_count` | `int` | Number of files actually copied |
| `hostname` | `str` | Machine hostname via `socket.gethostname()` |

### `SnapshotNotFoundError` (Exception)

Raised when a requested snapshot ID does not exist on disk.

## Steps

### Step 6.1 — Index Management

- `snapshot_dir_for(snapshot_id)` → `SNAPSHOTS_DIR / snapshot_id`
- `load_index()` → reads `SNAPSHOTS_DIR / INDEX_FILENAME`, returns `[]` if missing or corrupt
- `save_index(entries)` → writes JSON array of `SnapshotMeta` dicts to index file

### Step 6.2 — Snapshot Creation

`create_snapshot(entries, home, trigger, keep=5)`:
1. Generate `snapshot_id` from UTC now formatted as `YYYY-MM-DDTHH-MM-SS`
2. Create `SNAPSHOTS_DIR / snapshot_id/`
3. For each entry where `home / relative_path` exists: `shutil.copy2` preserving relative path
4. Build `SnapshotMeta` with `socket.gethostname()` for hostname
5. Append to index and save
6. Call `apply_retention(keep)`
7. Return the metadata

### Step 6.3 — Rollback

- `list_snapshots()` → `load_index()` sorted newest-first by `created_at`
- `rollback(snapshot_id, home, dry_run=False)`:
  1. Locate `SNAPSHOTS_DIR / snapshot_id` — raise `SnapshotNotFoundError` if missing
  2. Walk snapshot dir via `rglob("*")`, compute destination as `home / relative_path_within_snapshot`
  3. If `dry_run`: return paths without writing
  4. Copy each file back with `shutil.copy2`, creating parent dirs as needed
  5. Return list of restored paths
- `rollback_latest(home, dry_run=False)`:
  1. Get newest snapshot from `list_snapshots()`
  2. Raise `SnapshotNotFoundError` if no snapshots exist
  3. Delegate to `rollback()`

### Step 6.4 — Retention Policy

`apply_retention(keep)`:
- `keep=0` → no-op (keep all)
- Sort by `created_at` descending
- Delete snapshots beyond index `keep` (both directory via `shutil.rmtree` and index entry)
- Return list of deleted snapshot IDs

### Step 6.5 — Snapshot Integrity Check

`verify_snapshot(snapshot_id, entries)`:
- Raise `SnapshotNotFoundError` if snapshot directory missing
- Compare files in snapshot dir against manifest entries
- Return `{"complete": bool, "missing": [...], "extra": [...]}`

## Design Decisions

1. `SNAPSHOTS_DIR` is a module-level constant — tests monkeypatch it to `tmp_path`
2. `create_snapshot` takes `keep` parameter rather than reading config directly — keeps the function testable and decoupled
3. Retention runs automatically at the end of `create_snapshot`
4. `keep=0` means keep all — disables retention rather than deleting everything
5. No Git integration — snapshots are purely local filesystem operations

## Test Coverage

20 tests across 5 test classes:
- `TestIndexManagement` — 3 tests (empty load, roundtrip, dir path)
- `TestCreateSnapshot` — 4 tests (copy, skip missing, index update, metadata)
- `TestRollback` — 5 tests (restore, parent dirs, dry run, missing snapshot, latest)
- `TestRetention` — 5 tests (delete oldest, keep N, under limit, zero keeps all, index update)
- `TestVerifySnapshot` — 3 tests (complete, missing, extra)
