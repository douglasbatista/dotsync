# DotSync Architecture

## Overview

DotSync is a CLI tool for backing up, syncing, and encrypting configuration files (dotfiles) across Windows and Linux workstations. The tool uses a Git repository with git-crypt symmetric encryption for secure storage.

## Core Components

### 1. CLI Interface (`main.py`)

Entry point using Typer with Rich output. Seven commands with structured exit codes (0–5), global `--verbose` flag, and late imports per command.

- `init` — Initialize config + repo + git-crypt; options: `--repo-path`, `--remote`, `--llm-endpoint`
- `discover` — Scan, classify, and interactively resolve config files; option: `--no-ai`
- `sync` — Full pipeline: discover → flag → confirm sensitive → snapshot → sync → commit/push → health checks; options: `--dry-run`, `--no-push`, `--message`
- `restore` — Pull → snapshot → restore; or `--from-snapshot` for direct snapshot rollback; options: `--dry-run`, `--no-pull`
- `rollback` — Interactive or explicit snapshot rollback with integrity verification; options: `--dry-run`, `--list`
- `status` — Config summary, managed files count, snapshot count, git status
- `config` — View (`--show`) or update (`--set KEY=VALUE`) configuration with key validation

### 2. Configuration (`config.py`)

- Loads/saves TOML configuration from `~/.dotsync/config.toml`
- Uses Pydantic for schema validation
- Default configuration stored in `DotSyncConfig` dataclass

### 3. File Discovery (`discovery.py`)

- `ConfigFile` Pydantic model: path, size, include verdict, reason, os_profile
- `SAFETY_EXCLUDES`: security invariants (SSH keys, `.gnupg/`, `.dotsync/`, `dotsync.key`) — never included, enforced on extra paths too
- `PRUNE_DIRS`: ~30 directory names pruned by exact name match during walk (`.git`, `node_modules`, `__pycache__`, cache dirs, build dirs, etc.)
- `_PRUNE_PREFIXES`: multi-segment prefixes (`.local/share/`, `.local/lib/`) pruned by prefix match
- `BLOCKED_EXTENSIONS`: ~40 file extensions rejected at file level (lock files, databases, images, binaries, source code)
- `BLOCKED_FILENAMES`: exact filenames and glob patterns rejected at file level (`package-lock.json`, `*.log`, etc.)
- `HEURISTIC_RULES`: structural rules evaluated in order (home dotfile, XDG config, Windows AppData, config extension) with depth limits
- `ScanEvent` TypedDict and `ProgressCallback` type alias for real-time scan progress reporting
- `scan_candidates()`: uses `os.scandir()` with manual recursion via `_scan_dir()` for efficient scanning with `DirEntry` stat reuse. Scan roots walked in parallel via `ThreadPoolExecutor`. Two-phase filtering — Phase 1 prunes directory subtrees by name (`PRUNE_DIRS`) or prefix (`_PRUNE_PREFIXES`) + safety excludes. Phase 2 pre-filters files via `_prefilter_file()`: safety excludes, blocked extensions/filenames, size >50 KB, binary detection (512-byte check, runs last). Extra paths bypass pruning and blocked lists but not safety excludes. `PermissionError` on inaccessible dirs silently skipped. Accepts optional `progress` callback for live UI updates.
- `classify_heuristic()`: matches against heuristic rules (first match wins), user exclude/include patterns, and assigns `os_profile` (linux/windows/shared)
- `build_candidate_entry()`: constructs per-file payload dict (path, size, first_lines with 200-char cap, modified_days_ago) for LLM requests
- `_should_prune_dir()`: checks `PRUNE_DIRS` (name match), `_PRUNE_PREFIXES` (prefix match), safety excludes, and generated directory names (UUID, hex, numeric) via `_is_generated_filename()`
- `classify_with_ai()`: sends ambiguous files to LiteLLM proxy in batches of 20 (`MAX_CANDIDATES_PER_BATCH`), caches results in `~/.dotsync/classification_cache.json`, falls back to `ask_user` on error per batch
- `discover()`: orchestrator — scan → heuristic classify → AI classify (if endpoint set) → mark remaining ambiguous as `ask_user`. Accepts optional `progress` callback; emits `phase_start`/`phase_done` events for each pipeline stage.

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

Post-operation safety net — runs configurable shell commands after sync/restore to verify the system is still healthy. Automatically rolls back on failure.

- **Data model**: `HealthCheck` (name, command, timeout, expected exit code, enabled) and `HealthCheckResult` (pass/fail, exit code, stdout/stderr, duration)
- **Default checks**: `DEFAULT_CHECKS` verifies `git --version` and `${SHELL} -c 'echo ok'` — always run unless disabled
- **Single runner**: `run_check()` executes via `subprocess.run` with `shell=False`, `shlex.split` after `os.path.expandvars`; handles `TimeoutExpired` (exit=-1) and `FileNotFoundError` (exit=-2)
- **Batch runner**: `run_all_checks(cfg)` combines defaults + user checks from `cfg.health_checks` + optional extra checks; sequential execution for cascading failure detection
- **Auto-rollback**: `check_and_rollback_if_needed()` calls `snapshot.rollback()` when any check fails, then raises `HealthCheckFailedError` with failed check names and snapshot ID
- **Orchestration**: `post_operation_checks()` is the single integration point for sync/restore — runs all checks, triggers rollback on failure, logs results
- Custom exception: `HealthCheckFailedError`

### 9. UI (`ui.py`)

Rich terminal output helpers for consistent formatting across all commands.

- **Consoles**: `console` (stdout) and `err_console` (stderr, red)
- **Message helpers**: `print_success()` (green), `print_warning()` (yellow), `print_error()` (red/stderr), `print_section()` (bold rule)
- **Tables**: `file_table()` for ConfigFile lists (path, size, verdict, reason, OS), `snapshot_table()` for SnapshotMeta lists (numbered, with ID, date, trigger, file count, host)
- **Panels**: `flag_panel()` for sensitive file details (matches with line numbers and redacted previews, AI flag status)
- **Live scan display**: `ScanStats` dataclass with `start_time` field; `make_scan_display()` returns a `Group` of animated `Spinner("dots")` and stats table with elapsed time — auto-refreshes via `Rich.Live` even between events

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
- System prompt uses **environment vs infrastructure** framing: "Is this file part of the user's computing environment, or internal infrastructure the tool recreates on reinstall?"
- INCLUDE trigger: file reflects a user choice (plugins, themes, registry mirrors) or would change behavior if lost
- EXCLUDE trigger: tool would regenerate identical content on reinstall; project repo metadata; generated/machine-written content
