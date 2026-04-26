# DotSync Architecture

## Overview

DotSync is a CLI tool for backing up, syncing, and restoring configuration files (dotfiles) across Windows and Linux workstations. The tool uses a Git repository for storage, with optional AI-powered file classification.

## Core Components

### 1. CLI Interface (`main.py`)

Thin Typer entry point with Rich output. Delegates all business logic to `orchestrator.py`. Seven commands with structured exit codes (0–5), global `--verbose` flag, and late imports per command.

- `init` — Initialize config + repo; options: `--repo-path`, `--remote`, `--llm-endpoint`
- `discover` — Scan, classify, and interactively resolve config files; pre-flight LLM connectivity check; option: `--no-ai`
- `sync` — Delegates to `run_sync()`; options: `--dry-run`, `--no-push`, `--message`
- `restore` — Delegates to `run_restore()`; options: `--dry-run`, `--no-pull`, `--from-snapshot`
- `rollback` — Interactive or explicit snapshot rollback with integrity verification; options: `--dry-run`, `--list`
- `status` — Config summary, managed files count, snapshot count, git status
- `config` — View (`--show`) or update (`--set KEY=VALUE`) configuration with key validation
- `_check_llm_connectivity()` — pre-flight probe before AI triage; warns with reason and prompts continue/abort

### 2. Orchestration Layer (`orchestrator.py`)

Pure business logic, no Typer or Rich imports. Interaction callbacks are injected by the CLI so all prompts and tables stay in the presentation layer.

- `run_discover(cfg, *, resolve_pending, confirm_register, progress)` — scan → enforce NEVER_INCLUDE → resolve pending via callback → register new files. Sensitive data flagging is performed at sync time only.
- `run_sync(cfg, *, dry_run, no_push, message, resolve_sensitive, confirm_execute)` — load manifest → flag → snapshot → plan → execute → commit/push → health checks
- `run_restore(cfg, *, dry_run, no_pull, from_snapshot)` — pull → snapshot → plan → execute → health checks; or direct snapshot rollback
- Result dataclasses: `DiscoverResult`, `SyncResult`, `RestoreResult` — carry everything the CLI needs to render (counts, actions, snapshot metadata) without any I/O
- Helpers: `_manifest_to_config_files()`, `_resolve_sensitive_confirmations()`, `_mark_sensitive()`

### 3. Configuration (`config.py`)

- Loads/saves TOML configuration from `~/.dotsync/config.toml`
- Uses Pydantic for schema validation with `field_validator` decorators for automatic path expansion
- `expand_path(p, resolve)` — expands `~`, `$HOME`, `%USERPROFILE%` via `os.path.expandvars` + `Path.expanduser()`; applied to `repo_path`, `include_extra` (full resolve) and `exclude_patterns` (expanduser only, no resolve); `health_checks` left unexpanded (shell handles `~` at runtime)
- `llm_api_key` field — optional bearer token with `{env:VAR}` substitution support so secrets stay out of the config file
- Default configuration stored in `DotSyncConfig` dataclass

### 4. File Discovery (`discovery.py`)

- `ConfigFile` Pydantic model: path, size, include verdict, sensitive flag, reason, os_profile
- `SAFETY_EXCLUDES`: security invariants (SSH keys, `.gnupg/`, `.dotsync/`) — never included, enforced on extra paths too
- `PRUNE_DIRS`: ~30 directory names pruned by exact name match during walk (`.git`, `node_modules`, `__pycache__`, cache dirs, build dirs, etc.)
- `_PRUNE_PREFIXES`: multi-segment prefixes (`.local/share/`, `.local/lib/`) pruned by prefix match
- `ALLOWED_EXTENSIONS`: 14 config file extensions accepted by whitelist (`.toml`, `.yaml`, `.yml`, `.json`, `.jsonc`, `.ini`, `.cfg`, `.conf`, `.config`, `.xml`, `.properties`, `.env`, `.rc`, `.plist`)
- `ALLOWED_NAMED_FILES`: extensionless filenames accepted by whitelist (`config`, `credentials`)
- `HOME_BLOCKED_DOTFILES`: ~18 known noise dotfiles rejected at `$HOME` root (history files, auth tokens, session errors)
- `HEURISTIC_RULES`: structural rules evaluated in order (home dotfile, XDG config, Windows AppData, config extension) with depth limits
- `ScanEvent` TypedDict and `ProgressCallback` type alias for real-time scan progress reporting
- `scan_candidates(repo_path=...)`: uses `os.scandir()` with manual recursion via `_scan_dir()` for efficient scanning with `DirEntry` stat reuse. Scan roots walked in parallel via `ThreadPoolExecutor`. Two-phase filtering — Phase 1 prunes directory subtrees by name (`PRUNE_DIRS`), prefix (`_PRUNE_PREFIXES`), safety excludes, generated dir names, or resolved `repo_path` match. Phase 2 pre-filters files via `_prefilter_file()`: safety excludes, size >50 KB, whitelist gate (`ALLOWED_EXTENSIONS` / `ALLOWED_NAMED_FILES` / extensionless home dotfiles), binary detection (512-byte check, runs last). Home-root dotfiles get special handling: accepted if extensionless or allowed extension, rejected if in `HOME_BLOCKED_DOTFILES`. Extra paths bypass pruning and whitelist but not safety excludes. `PermissionError` on inaccessible dirs silently skipped. Accepts optional `progress` callback for live UI updates.
- `classify_heuristic()`: matches against heuristic rules (first match wins), user exclude/include patterns, and assigns `os_profile` (linux/windows/shared). Tags matching reason but leaves `include=None` — AI gets final say on all files. User exclude/include patterns are still deterministic.
- `build_candidate_entry()`: constructs per-file payload dict (path, size, first_lines with 200-char cap, modified_days_ago) for LLM requests
- `_should_prune_dir()`: checks `PRUNE_DIRS` (name match), `_PRUNE_PREFIXES` (prefix match), safety excludes, `repo_path` (resolved path comparison), and generated directory names (UUID, hex 8+, numeric) via `_is_generated_filename()`. Generated filename detection is directory-level only — not applied to individual files.
- `classify_with_ai()`: sends unresolved files to LiteLLM proxy in batches of 10 (`MAX_CANDIDATES_PER_BATCH`), caches results in `~/.dotsync/classification_cache.json`. Failed batches are marked `ai:unreachable` individually — remaining batches continue processing. All batch details (paths, timing, verdicts, errors) logged at DEBUG level.
- `discover()`: orchestrator — scan → heuristic classify → AI classify (if endpoint set) → heuristic-matched files with no AI verdict fall back to `include=True` → remaining ambiguous marked `ask_user`. Accepts optional `progress` callback; emits `phase_start`/`phase_done` events for each pipeline stage.

### 5. Flagging (`flagging.py`)

Content-based sensitive data detection for files marked `include=True` by discovery. Defense-in-depth layer before files enter the git repo.

- `SENSITIVE_PATTERNS`: 11 compiled regexes (GitHub tokens, AWS keys, OpenAI/Anthropic keys, PEM blocks, connection strings, generic token/api_key, email)
- `NEVER_INCLUDE`: hardcoded blocklist (`.ssh/id_rsa`, `.ssh/id_ed25519`, `.ssh/id_ecdsa`, `.gnupg/`) — defense-in-depth behind `SAFETY_EXCLUDES`
- `scan_file_for_secrets(path)`: line-by-line regex scan, skips `#`-commented lines for generic patterns, redacts matched values in preview
- `ai_flag_check(path, cfg)`: sends first 30 lines to LLM for sensitivity assessment, caches results by `{path}:{mtime}`, fails open on error
- `flag_all(files, cfg)`: orchestrator — scans included files, only calls AI when no regex matches found, returns `FlagResult` with `requires_confirmation` flag
- `enforce_never_include(files)`: mutates files matching `NEVER_INCLUDE` to `include=False, reason="never_include"`

### 6. Git Operations (`git_ops.py`)

Storage backbone — manages the dotfiles Git repository.

- **Dependency checks**: `check_dependencies()` verifies `git` is on PATH with platform-specific install hints
- **Repo init**: `init_repo(cfg)` creates/opens repo, writes `.gitattributes`, empty `.dotsync_manifest.json`, initial commit; idempotent
- **Remote management**: `set_remote()` creates/updates origin; `get_remote()` returns URL or `None`
- **Manifest**: `ManifestEntry` dataclass tracks `relative_path`, `os_profile`, `added_at`, `sensitive_flagged`; CRUD via `load_manifest()`, `save_manifest()`, `add_to_manifest()` (dedup by path), `remove_from_manifest()`
- **Commit/push/pull**: `commit_and_push()` stages all, commits, pushes (raises `NoRemoteConfiguredError` if no origin); `pull()` fetches and checks `unmerged_blobs()` for `MergeConflictError`
- **File copying**: `copy_to_repo()` copies file preserving relative path structure and metadata via `shutil.copy2`
- Custom exceptions: `MissingDependencyError`, `NoRemoteConfiguredError`, `MergeConflictError`

### 7. Sync Engine (`sync.py`)

Orchestrates file operations between the home directory and the dotfiles repository.

- **OS profile filter**: `filter_by_profile()` returns entries matching `current_os` or `"shared"`
- **Path transformer**: `transform_paths()` rewrites home-directory paths in file content across platforms (Linux ↔ Windows), matching only value positions (after `=`, `:`, or in quotes) to avoid mangling URLs
- **Sync (home → repo)**: `SyncAction` dataclass; `plan_sync()` filters by profile and checks file existence; `execute_sync()` copies files with `shutil.copy2`, supports dry-run
- **Restore (repo → home)**: `RestoreAction` dataclass; `plan_restore()` checks profile and repo file existence; `execute_restore()` copies files, optionally applying cross-platform path transforms for shared files
- **New file registration**: `register_new_files()` accepts pre-confirmed files from the CLI layer, copies to repo and adds manifest entries with `sensitive_flagged` propagated from `ConfigFile.sensitive`; supports dry-run
- **Conflict detection**: `detect_conflicts()` compares mtime of local and repo copies against `last_sync` — conflict when both sides modified after last sync

### 8. Snapshots (`snapshot.py`)

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

### 9. Health Checks (`health.py`)

Post-operation safety net — runs configurable shell commands after sync/restore to verify the system is still healthy. Automatically rolls back on failure.

- **Data model**: `HealthCheck` (name, command, timeout, expected exit code, enabled) and `HealthCheckResult` (pass/fail, exit code, stdout/stderr, duration)
- **Default checks**: `DEFAULT_CHECKS` verifies `git --version` and `${SHELL} -c 'echo ok'` — always run unless disabled
- **Single runner**: `run_check()` executes via `subprocess.run` with `shell=False`, `shlex.split` after `os.path.expandvars`; handles `TimeoutExpired` (exit=-1) and `FileNotFoundError` (exit=-2)
- **Batch runner**: `run_all_checks(cfg)` combines defaults + user checks from `cfg.health_checks` + optional extra checks; sequential execution for cascading failure detection
- **Auto-rollback**: `check_and_rollback_if_needed()` calls `snapshot.rollback()` when any check fails, then raises `HealthCheckFailedError` with failed check names and snapshot ID
- **Orchestration**: `post_operation_checks()` is the single integration point for sync/restore — runs all checks, triggers rollback on failure, logs results
- Custom exception: `HealthCheckFailedError`

### 10. UI (`ui.py`)

Rich terminal output helpers for consistent formatting across all commands.

- **Consoles**: `console` (stdout) and `err_console` (stderr, red)
- **Message helpers**: `print_success()` (green), `print_warning()` (yellow), `print_error()` (red/stderr), `print_section()` (bold rule)
- **Tables**: `file_table()` for ConfigFile lists (path, size, verdict, reason, OS), `snapshot_table()` for SnapshotMeta lists (numbered, with ID, date, trigger, file count, host)
- **Panels**: `flag_panel()` for sensitive file details (matches with line numbers and redacted previews, AI flag status)
- **Live scan display**: `ScanStats` dataclass with `start_time` field; `make_scan_display()` returns a `Group` of animated `Spinner("dots")` and stats table with elapsed time — auto-refreshes via `Rich.Live` even between events

## Data Flow

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────┐
│   User CLI  │────▶│  Orchestrator    │────▶│   Config    │
│  (main.py)  │     │ (orchestrator.py)│     └─────────────┘
└─────────────┘     └──────────────────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
       ┌────────────┐ ┌──────────┐ ┌──────────┐
       │ Discovery  │ │ Flagging │ │ Snapshot │
       └────────────┘ └──────────┘ └──────────┘
              │
              ▼
       ┌────────────┐     ┌─────────────┐
       │    Sync    │────▶│  Git Repo   │
       └────────────┘     └─────────────┘
```

## Security Model

- Multi-layer secret protection prevents secrets from reaching the repository
- Safety excludes block SSH keys and `.gnupg/` at the discovery layer
- Regex and AI scanning flag sensitive files at sync time, before they are committed to the repo
- NEVER_INCLUDE blocklist provides a backstop at discovery time
- Users must consciously decide to include files flagged as sensitive during sync
- Discover focuses on classification and registration; sensitive data verification happens when syncing to the repository

## AI Triage (Optional)

- LiteLLM proxy endpoint for AI-powered file triage
- Configurable via `llm_endpoint`, `llm_api_key` (supports `{env:VAR}` substitution), and `llm_model` settings
- `probe_llm()` — pre-flight connectivity check before discover; returns `(ok, reason)` so failures show auth error / wrong model / connection refused / timeout
- Uses plain httpx for API calls with retry logic (2 retries, exponential backoff 2s/4s, 90s timeout)
- Batches of 10 files per request; failed batches don't block remaining batches
- System prompt uses **environment vs infrastructure** framing: "Is this file part of the user's computing environment, or internal infrastructure the tool recreates on reinstall?"
- INCLUDE trigger: file reflects a user choice (plugins, themes, registry mirrors) or would change behavior if lost — but settings.json/config.json require content inspection (may be app state)
- EXCLUDE trigger: tool would regenerate identical content on reinstall; project repo metadata; generated/machine-written content; server-pushed feature flags; addon-shipped default settings; IDE internal storage; OEM bloatware; VPN auto-generated settings; build scaffolding; project/file history
- Heuristic classifier pre-tags files but defers final verdict to AI; falls back to heuristic include when AI is unavailable
- All AI batch details (timing, verdicts, errors) logged at DEBUG level via `--verbose` / `dotsync.log`
