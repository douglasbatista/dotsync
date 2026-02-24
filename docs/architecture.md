# DotSync Architecture

## Overview

DotSync is a CLI tool for backing up, syncing, and encrypting configuration files (dotfiles) across Windows and Linux workstations. The tool uses a Git repository with git-crypt symmetric encryption for secure storage.

## Core Components

### 1. CLI Interface (`main.py`)

Entry point using Typer. Commands:
- `init` - Initialize configuration with confirmation for existing configs
- `sync` - Sync configuration files with the repository
- `restore` - Restore configuration files from the repository
- `rollback` - Rollback to a previous snapshot
- `status` - Show current repository status

### 2. Configuration (`config.py`)

- Loads/saves TOML configuration from `~/.dotsync/config.toml`
- Uses Pydantic for schema validation
- Default configuration stored in `DotSyncConfig` dataclass

### 3. File Discovery (`discovery.py`)

- `ConfigFile` Pydantic model: path, size, include verdict, reason, os_profile
- `SAFETY_EXCLUDES`: security invariants (SSH keys, `.gnupg/`, `.dotsync/`, `dotsync.key`) — never included, enforced on extra paths too
- `SCAN_EXCLUDES`: noise directories (`.cache/`, `node_modules/`, `__pycache__/`, etc.) — pruned during walk
- `HEURISTIC_RULES`: structural rules evaluated in order (home dotfile, XDG config, Windows AppData, config extension) with depth limits
- `scan_candidates()`: walks `config_dirs()` roots up to depth 5, skips symlinks, files >512 KB, binary files, safety excludes, and prunes scan-excluded dirs
- `classify_heuristic()`: matches against heuristic rules (first match wins), user exclude/include patterns, and assigns `os_profile` (linux/windows/shared)
- `classify_with_ai()`: sends ambiguous files to LiteLLM proxy, caches results in `~/.dotsync/classification_cache.json`, falls back to `ask_user` on error
- `discover()`: orchestrator — scan → heuristic classify → AI classify (if endpoint set) → mark remaining ambiguous as `ask_user`

### 4. Flagging (`flagging.py`)

Content-based sensitive data detection for files marked `include=True` by discovery. Defense-in-depth layer before files enter the git repo.

- `SENSITIVE_PATTERNS`: 11 compiled regexes (GitHub tokens, AWS keys, OpenAI/Anthropic keys, PEM blocks, connection strings, generic token/api_key, email)
- `NEVER_INCLUDE`: hardcoded blocklist (`.ssh/id_rsa`, `.ssh/id_ed25519`, `.ssh/id_ecdsa`, `.gnupg/`, `dotsync_key`) — defense-in-depth behind `SAFETY_EXCLUDES`
- `scan_file_for_secrets(path)`: line-by-line regex scan, skips `#`-commented lines for generic patterns, redacts matched values in preview
- `ai_flag_check(path, cfg)`: sends first 30 lines to LLM for sensitivity assessment, caches results by `{path}:{mtime}`, fails open on error
- `flag_all(files, cfg)`: orchestrator — scans included files, only calls AI when no regex matches found, returns `FlagResult` with `requires_confirmation` flag
- `enforce_never_include(files)`: mutates files matching `NEVER_INCLUDE` to `include=False, reason="never_include"`

### 5. Git Operations (`git_ops.py`)

Storage backbone — manages the dotfiles Git repository and git-crypt encryption.

- **Dependency checks**: `check_dependencies()` verifies `git` and `git-crypt` are on PATH with platform-specific install hints
- **Repo init**: `init_repo(cfg)` creates/opens repo, writes `.gitattributes` (git-crypt catch-all + exclusions), empty `.dotsync_manifest.json`, initial commit; idempotent
- **git-crypt**: `init_gitcrypt()` runs `git-crypt init` + `export-key` via subprocess; `unlock_gitcrypt()` runs `git-crypt unlock`; errors wrapped as `GitCryptError`
- **Remote management**: `set_remote()` creates/updates origin; `get_remote()` returns URL or `None`
- **Manifest**: `ManifestEntry` dataclass tracks `relative_path`, `os_profile`, `added_at`, `sensitive_flagged`; CRUD via `load_manifest()`, `save_manifest()`, `add_to_manifest()` (dedup by path), `remove_from_manifest()`
- **Commit/push/pull**: `commit_and_push()` stages all, commits, pushes (raises `NoRemoteConfiguredError` if no origin); `pull()` fetches and checks `unmerged_blobs()` for `MergeConflictError`
- **File copying**: `copy_to_repo()` copies file preserving relative path structure and metadata via `shutil.copy2`
- Custom exceptions: `MissingDependencyError`, `GitCryptError`, `NoRemoteConfiguredError`, `MergeConflictError`

### 6. Sync Engine (`sync.py`)

Orchestrates file operations between the home directory and the dotfiles repository.

- **OS profile filter**: `filter_by_profile()` returns entries matching `current_os` or `"shared"`
- **Path transformer**: `transform_paths()` rewrites home-directory paths in file content across platforms (Linux ↔ Windows), matching only value positions (after `=`, `:`, or in quotes) to avoid mangling URLs
- **Sync (home → repo)**: `SyncAction` dataclass; `plan_sync()` filters by profile and checks file existence; `execute_sync()` copies files with `shutil.copy2`, supports dry-run
- **Restore (repo → home)**: `RestoreAction` dataclass; `plan_restore()` checks profile and repo file existence; `execute_restore()` copies files, optionally applying cross-platform path transforms for shared files
- **New file registration**: `register_new_files()` accepts pre-confirmed files from the CLI layer, copies to repo and adds manifest entries; supports dry-run
- **Conflict detection**: `detect_conflicts()` compares mtime of local and repo copies against `last_sync` — conflict when both sides modified after last sync

### 7. Snapshots (`snapshot.py`)

Local-only timestamped backups of managed files, created automatically before any write operation (sync/restore).

- **Storage**: `~/.dotsync/snapshots/<snapshot_id>/` with relative path structure mirroring home directory
- **Index**: `snapshot_index.json` tracks `SnapshotMeta` entries (id, created_at, trigger, file_count, hostname)
- **Creation**: `create_snapshot()` copies each manifest entry that exists under home, using `shutil.copy2` for metadata preservation; auto-runs retention
- **Rollback**: `rollback(snapshot_id)` restores files from a snapshot directory back to home; `rollback_latest()` delegates to the newest snapshot; both support `dry_run` mode
- **Listing**: `list_snapshots()` returns all snapshots sorted newest-first
- **Retention**: `apply_retention(keep)` deletes oldest snapshots beyond the limit; `keep=0` disables (keeps all)
- **Integrity**: `verify_snapshot()` checks a snapshot against manifest entries, reporting missing and extra files
- Custom exception: `SnapshotNotFoundError`
- Snapshots never enter the Git repository

### 8. Health Checks (`health.py`)

- Runs configured health check commands
- Validates sync operations completed successfully

### 9. UI (`ui.py`)

- Terminal UI components
- Progress indicators and status displays

## Data Flow

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   User CLI  │────▶│   Config    │────▶│  Discovery  │
└─────────────┘     └─────────────┘     └─────────────┘
                                              │
                                              ▼
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Git Repo   │◀────│    Sync     │◀────│  Snapshot   │
└─────────────┘     └─────────────┘     └─────────────┘
       │
       ▼
┌─────────────┐
│ git-crypt   │
└─────────────┘
```

## Security Model

- All sensitive data encrypted using git-crypt symmetric encryption
- Encryption key stored separately from repository
- No secrets stored in configuration files

## AI Triage (Optional)

- LiteLLM proxy endpoint for AI-powered file triage
- Configurable via `llm_endpoint` and `llm_model` settings
- Uses plain httpx for API calls
