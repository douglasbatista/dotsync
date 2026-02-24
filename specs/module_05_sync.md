# Module 05 — Sync Engine

## Overview

The sync engine orchestrates file operations between the home directory and the dotfiles repository. It handles sync (home → repo), restore (repo → home), OS profile filtering, cross-platform path transformation, dry-run mode, new file registration, and conflict detection.

## Dependencies

- `git_ops.ManifestEntry` — manifest entries with `relative_path`, `os_profile`, `added_at`, `sensitive_flagged`
- `git_ops.copy_to_repo()` — file copy preserving relative paths
- `git_ops.add_to_manifest()` — deduplicated manifest insertion
- `git_ops.load_manifest()` — load manifest entries
- `discovery.ConfigFile` — Pydantic model with `path`, `abs_path`, `include`, `os_profile`
- `flagging.FlagResult` — `config_file`, `requires_confirmation`
- `config.DotSyncConfig` — `repo_path` field
- `config.CONFIG_DIR` — `~/.dotsync/` for state file

## Steps

### Step 5.1 — OS Profile Filter

`filter_by_profile(entries, current_os)` returns entries where `os_profile == current_os` or `os_profile == "shared"`.

### Step 5.2 — Path Transformer

`transform_paths(content, source_os, target_os, source_home, target_home)`:
- No-op when `source_os == target_os`
- Matches `source_home` in value positions (after `=`, `:`, or inside quotes)
- Replaces with `target_home`, flipping path separators in the replaced segment
- Protects URLs (`https://`, `http://`) from transformation

### Step 5.3 — Sync (Home → Repo)

`SyncAction` dataclass with `source`, `destination`, `action` (`copy`/`skip_missing`/`skip_excluded`), `transformed`.

`plan_sync(entries, home, repo_path, current_os)`:
1. Filter entries by OS profile
2. For each entry: resolve source in home, check existence
3. Mark `copy` or `skip_missing`

`execute_sync(actions, dry_run)`:
1. Filter to `copy` actions
2. If dry_run: return unchanged
3. Otherwise: `shutil.copy2(source, destination)` with parent dir creation

### Step 5.4 — Restore (Repo → Home)

`RestoreAction` dataclass with `source` (in repo), `destination` (in home), `action` (`restore`/`skip_missing_in_repo`/`skip_profile`), `transformed`.

`plan_restore(entries, home, repo_path, current_os)`:
1. Check OS profile — skip if not matching and not shared
2. Check if repo file exists — skip if not
3. Otherwise mark `restore`

`execute_restore(actions, dry_run, source_os, target_os, source_home, target_home)`:
1. If dry_run: return unchanged
2. For `restore` actions: create parent dirs, copy file
3. If `transformed=True` and transform params provided: read content, apply `transform_paths`, write text instead of binary copy

### Step 5.5 — New File Registration

`register_new_files(new_files, flag_results, repo_path, home, cfg, dry_run)`:
1. Build confirmed set from `flag_results` where `requires_confirmation == False`
2. For each file in `new_files` that's confirmed and `include == True`:
   - Create `ManifestEntry` with current timestamp
   - If not dry_run: `copy_to_repo()` and `add_to_manifest()`
3. Return list of new entries

### Step 5.6 — Conflict Detection

`Conflict` dataclass with `relative_path`, `local_mtime`, `repo_mtime`.

`detect_conflicts(entries, home, repo_path, last_sync)`:
- For each entry: get mtime of local and repo copies
- If both mtimes > `last_sync` → conflict
- Skip if either file doesn't exist

## Testing

24 tests covering all functions. All tests use `tmp_path`, no real git operations, no network calls.

## Key Design Decisions

1. **Path transform scope**: Only transform paths in value positions (after `=`, `:`, or in quotes) to avoid mangling URLs and other content.
2. **execute_sync does its own copy**: Uses `shutil.copy2` directly rather than always delegating to `git_ops.copy_to_repo`, because transform cases need to write transformed content.
3. **register_new_files accepts pre-filtered list**: No user I/O — receives already-confirmed files from the CLI layer.
4. **Conflict detection is mtime-based**: Both local and repo mtimes must exceed `last_sync` for a conflict.
