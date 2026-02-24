# Project Status

## Current Milestone: Health Checks Complete ✅

### Completed
- [x] Project scaffolding with uv
- [x] CLI entry point using Typer
- [x] Configuration schema with Pydantic
- [x] `init` command implementation
  - [x] Creates default configuration
  - [x] Prompts for confirmation when overwriting existing config
- [x] Core module structure created
- [x] Documentation structure established
- [x] File discovery and classification (`discovery.py`)
  - [x] `ConfigFile` Pydantic model
  - [x] `scan_candidates()` with depth/size/binary/safety-exclude/scan-exclude filtering
  - [x] `classify_heuristic()` with structural heuristic rules (home dotfile, XDG, AppData, config extension)
  - [x] `classify_with_ai()` with LiteLLM proxy and persistent cache
  - [x] `discover()` orchestrator
  - [x] 35 tests with full acceptance criteria coverage
- [x] Sensitive data flagging (`flagging.py`)
  - [x] 11 compiled regex patterns for secret detection
  - [x] `NEVER_INCLUDE` defense-in-depth blocklist
  - [x] `scan_file_for_secrets()` line-by-line scanner
  - [x] `ai_flag_check()` with LLM integration and mtime-keyed cache
  - [x] `flag_all()` orchestrator
  - [x] `enforce_never_include()` blocklist enforcement
  - [x] 21 tests with full coverage
- [x] Git & git-crypt integration (`git_ops.py`)
  - [x] `check_dependencies()` with platform-specific install hints
  - [x] `init_repo()` — idempotent repo init with `.gitattributes` and manifest
  - [x] `init_gitcrypt()` / `unlock_gitcrypt()` — subprocess wrappers
  - [x] `set_remote()` / `get_remote()` — origin management
  - [x] `ManifestEntry` with CRUD functions (dedup, filter)
  - [x] `commit_and_push()` / `pull()` with conflict detection
  - [x] `copy_to_repo()` — preserves relative paths and metadata
  - [x] 24 tests with full coverage

- [x] Sync engine (`sync.py`)
  - [x] `filter_by_profile()` — OS profile filtering
  - [x] `transform_paths()` — cross-platform path transformation with URL protection
  - [x] `SyncAction` / `plan_sync()` / `execute_sync()` — home → repo sync
  - [x] `RestoreAction` / `plan_restore()` / `execute_restore()` — repo → home restore
  - [x] `register_new_files()` — new file registration
  - [x] `Conflict` / `detect_conflicts()` — mtime-based conflict detection
  - [x] 24 tests with full coverage

- [x] Snapshot & rollback (`snapshot.py`)
  - [x] `SnapshotMeta` dataclass and `SnapshotNotFoundError`
  - [x] Index management (`load_index`, `save_index`, `snapshot_dir_for`)
  - [x] `create_snapshot()` — timestamped file backup with auto-retention
  - [x] `list_snapshots()` — newest-first listing
  - [x] `rollback()` / `rollback_latest()` — file restoration with dry-run
  - [x] `apply_retention()` — configurable cleanup (keep=0 disables)
  - [x] `verify_snapshot()` — integrity verification
  - [x] 20 tests with full coverage

- [x] Health checks (`health.py`)
  - [x] `HealthCheck` and `HealthCheckResult` dataclasses
  - [x] `DEFAULT_CHECKS` (git, shell)
  - [x] `run_check()` — single check runner with timeout/not-found handling
  - [x] `run_all_checks()` — batch runner (defaults + user + extras)
  - [x] `all_passed()` — result predicate
  - [x] `check_and_rollback_if_needed()` — auto-rollback trigger
  - [x] `post_operation_checks()` — single integration point
  - [x] `HealthCheckFailedError` exception
  - [x] 19 tests with full coverage

### In Progress
- (none)

### Pending
- [ ] UI/progress indicators
- [ ] `sync` command implementation
- [ ] `restore` command implementation
- [ ] `rollback` command implementation
- [ ] `status` command implementation
- [ ] Integration tests
- [ ] End-to-end testing

## Next Steps

1. Wire up CLI commands (`sync`, `restore`, `rollback`, `status`)
2. UI/progress indicators
3. Integration and end-to-end testing
