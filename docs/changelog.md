# Changelog

## [Unreleased]

### Added
- Initial project structure with Typer CLI
- Configuration management with Pydantic validation
- `init` command with overwrite confirmation for existing configs
- Core module stubs: `discovery`, `flagging`, `git_ops`, `sync`, `snapshot`, `health`, `ui`
- File discovery and classification (`discovery.py`)
  - `ConfigFile` model with path, size, verdict, reason, and os_profile
  - `scan_candidates()` with depth limit, size limit, binary detection, and hardcoded excludes
  - `classify_rule_based()` with known-file allowlists, user patterns, and OS profile detection
  - `classify_with_ai()` with LiteLLM proxy integration and persistent JSON cache
  - `discover()` orchestrator combining all classification stages
- 23 tests covering discovery module (allowlists, scanning, rule classification, AI classification, orchestrator, cache persistence)

### Changed
- Refactored discovery module to use structural heuristic rules instead of hardcoded tool-name allowlists
  - Replaced `KNOWN_FILES`/`KNOWN_DIRS` with `HEURISTIC_RULES` (home dotfile, XDG config, Windows AppData, config extension)
  - Split `HARDCODED_EXCLUDES` into `SAFETY_EXCLUDES` (security invariants) and `SCAN_EXCLUDES` (noise directories)
  - Renamed `classify_rule_based()` → `classify_heuristic()`, now takes `DotSyncConfig` directly
  - Updated constants: `MAX_DEPTH` 4→5, `MAX_FILE_SIZE` 1 MB→512 KB
  - Extra paths now respect `SAFETY_EXCLUDES` (security fix)
  - Directory pruning via `SCAN_EXCLUDES` for faster scanning
- `llm_client.chat_completion()` timeout parameter is now positional (`int`, was keyword-only `float`)
- Test suite expanded from 23 to 35 tests

### Added (continued)
- Sensitive data flagging (`flagging.py`)
  - `SENSITIVE_PATTERNS`: 11 compiled regexes for secret detection (GitHub, AWS, OpenAI, Anthropic, PEM, connection strings, generic token/api_key, email)
  - `NEVER_INCLUDE` defense-in-depth blocklist for SSH keys, `.gnupg/`, and `dotsync_key`
  - `scan_file_for_secrets()` with line-by-line scanning, comment skipping, and redacted previews
  - `ai_flag_check()` with LLM sensitivity assessment and mtime-keyed cache
  - `flag_all()` orchestrator with smart AI skip (no AI call when regex already matched)
  - `enforce_never_include()` blocklist enforcement
- 21 tests covering patterns, scanning, AI flagging, orchestration, never-include enforcement, and redaction

### Added (continued)
- Git & git-crypt integration (`git_ops.py`)
  - `check_dependencies()` with platform-specific install hints (linux/windows)
  - `init_repo()` — idempotent repo creation with `.gitattributes` and manifest
  - `init_gitcrypt()` / `unlock_gitcrypt()` — subprocess wrappers with `GitCryptError`
  - `set_remote()` / `get_remote()` — origin remote management
  - `ManifestEntry` dataclass with `load_manifest()`, `save_manifest()`, `add_to_manifest()`, `remove_from_manifest()`
  - `commit_and_push()` — stage all, commit, push with clean-tree skip
  - `pull()` — fetch with `MergeConflictError` on unmerged blobs
  - `copy_to_repo()` — copy files preserving relative paths and metadata
  - Custom exceptions: `MissingDependencyError`, `GitCryptError`, `NoRemoteConfiguredError`, `MergeConflictError`
- 24 tests covering dependency checks, repo init, git-crypt, remotes, manifest, push/pull, file copying

### Added (continued)
- Sync engine (`sync.py`)
  - `filter_by_profile()` — OS profile filtering (shared + current OS)
  - `transform_paths()` — cross-platform path transformation (Linux ↔ Windows) with URL protection
  - `SyncAction` / `plan_sync()` / `execute_sync()` — home → repo sync with dry-run support
  - `RestoreAction` / `plan_restore()` / `execute_restore()` — repo → home restore with optional path transforms
  - `register_new_files()` — new file registration from discovery/flagging pipeline
  - `Conflict` / `detect_conflicts()` — mtime-based conflict detection
- 24 tests covering profile filtering, path transforms, sync, restore, registration, and conflict detection

### Added (continued)
- Snapshot & rollback (`snapshot.py`)
  - `SnapshotMeta` dataclass and `SnapshotNotFoundError` exception
  - `SNAPSHOTS_DIR` constant (`~/.dotsync/snapshots/`) and JSON index management
  - `create_snapshot()` — timestamped backup of managed files before sync/restore with automatic retention
  - `list_snapshots()` — list all snapshots sorted newest-first
  - `rollback()` / `rollback_latest()` — restore files from a snapshot with dry-run support
  - `apply_retention(keep)` — delete oldest snapshots beyond limit; `keep=0` keeps all
  - `verify_snapshot()` — integrity check against manifest (missing/extra file detection)
- 20 tests covering index management, snapshot creation, rollback, retention, and integrity verification

### Added (continued)
- Health checks (`health.py`)
  - `HealthCheck` and `HealthCheckResult` dataclasses
  - `DEFAULT_CHECKS`: git and shell availability checks
  - `run_check()` — single check runner with timeout and command-not-found handling
  - `run_all_checks()` — batch runner combining defaults, user checks, and extras
  - `all_passed()` — convenience predicate for results
  - `check_and_rollback_if_needed()` — auto-rollback trigger with `snapshot.rollback()`
  - `post_operation_checks()` — single integration point for sync/restore
  - `HealthCheckFailedError` exception with failed check summary
- 19 tests covering data model, runner, batch, rollback, and orchestration

### Added (continued)
- CLI interface (`main.py`)
  - `init` command with `--repo-path`, `--remote`, `--llm-endpoint` options and dependency checking
  - `discover` command with `--no-ai` option and interactive resolution of pending files
  - `sync` command with `--dry-run`, `--no-push`, `--message` — full pipeline orchestration
  - `restore` command with `--dry-run`, `--no-pull`, `--from-snapshot` — pull + restore or direct snapshot rollback
  - `rollback` command with interactive snapshot selection and integrity verification
  - `status` command with config summary, managed files count, and snapshot count
  - `config` command with `--show` and `--set KEY=VALUE` (validates key names against schema)
  - `confirm_sensitive_files()` — interactive Include/Exclude/Skip for flagged files
  - Structured exit codes: 0 (success), 1 (health check failed), 2 (dependency missing), 3 (config not found), 4 (merge conflict), 5 (user aborted)
- Rich UI helpers (`ui.py`)
  - `print_success()`, `print_warning()`, `print_error()`, `print_section()` output helpers
  - `file_table()`, `snapshot_table()`, `flag_panel()` for Rich display
- 7 tests covering confirmation flow, config command, and error handling exit codes

### Changed (continued)
- Discovery module: AI classification improvements
  - `_read_first_lines()` now returns a joined string (was `list[str]`) with a 200-char total cap; appends `"..."` on truncation
  - Extracted `build_candidate_entry(cf)` helper for per-file payload construction
  - `classify_with_ai()` now batches candidates into chunks of 20 (`MAX_CANDIDATES_PER_BATCH`) instead of a single API call
  - Added constants: `MAX_FIRST_LINES`, `MAX_FIRST_LINES_CHARS`, `MAX_CANDIDATES_PER_BATCH`
- Test suite expanded from 30 to 33 tests in `test_discovery.py`

### Fixed
- None
