# Project Status

## Current Milestone: CLI Interface Complete

### Completed
- [x] Project scaffolding with uv
- [x] CLI entry point using Typer
- [x] Configuration schema with Pydantic
  - [x] `expand_path()` utility for `~` / `$HOME` / `%USERPROFILE%` expansion
  - [x] Pydantic `field_validator` decorators: `repo_path`/`gitcrypt_key_path` (expand + resolve), `include_extra` (expand + resolve each), `exclude_patterns` (expanduser only, no resolve), `health_checks` (no expansion)
  - [x] `include_extra` typed as `list[Path]` (was `list[str]`)
- [x] `init` command implementation
  - [x] Creates default configuration
  - [x] Prompts for confirmation when overwriting existing config
- [x] Core module structure created
- [x] Documentation structure established
- [x] File discovery and classification (`discovery.py`)
  - [x] `ConfigFile` Pydantic model (includes `sensitive: bool` field for flagging persistence)
  - [x] `scan_candidates(repo_path=...)` with `os.scandir()` + `_scan_dir()` recursive scanner, parallel root scanning via `ThreadPoolExecutor`, per-root max depth, two-phase filtering: subtree pruning (`PRUNE_DIRS` + `_PRUNE_PREFIXES` + `repo_path` + generated dir names) and `_prefilter_file()` with whitelist gate (safety excludes, size >50 KB, `ALLOWED_EXTENSIONS` / `ALLOWED_NAMED_FILES` / extensionless home dotfiles, `HOME_BLOCKED_DOTFILES`, 512-byte binary detection)
  - [x] `HOME_SCAN_DEPTH = 1` — `$HOME` scanned shallowly (direct children only), 21 `KNOWN_CONFIG_SUBDIRS` get deep scan (XDG, shell, editors, dev tools)
  - [x] `config_dirs()` returns `list[tuple[Path, int]]` with per-root max depth, existence-checked subdirs, `XDG_CONFIG_HOME` support
  - [x] `classify_heuristic()` with structural heuristic rules (home dotfile, XDG, AppData, config extension) — tags reason but leaves `include=None` for AI to decide
  - [x] `classify_with_ai()` with LiteLLM proxy, persistent cache, batch chunking (max 10 per call), retry with exponential backoff (2s, 4s), continue-on-failure (failed batch doesn't kill remaining)
  - [x] `build_candidate_entry()` helper with 200-char truncation on first_lines
  - [x] `discover()` orchestrator — heuristic-matched files fall back to `include=True` when no AI endpoint
  - [x] `ScanEvent` TypedDict and `ProgressCallback` for real-time progress reporting
  - [x] Progress events: `root_start`/`root_done`, `dir_enter`/`dir_pruned`, `file_accepted`/`file_rejected`, `phase_start`/`phase_done`, `ai_batch`
  - [x] `BLOCKED_FILENAME_PATTERNS` with regex detection for UUID (incl. dot-prefix), hex 8+ (incl. @version), numeric, hex-with-dots, trailing timestamp, embedded hex 32+ filenames
  - [x] `_is_generated_filename()` used in `_should_prune_dir()` for directory-level pruning only (removed from file-level filtering)
  - [x] `_should_prune_dir()` prunes directories with generated names (UUID, hex, numeric) via `_is_generated_filename()`
  - [x] AI system prompt uses environment-vs-infrastructure framing with detailed EXCLUDE rules (feature flags, addon defaults, IDE internals, OEM bloatware, VPN dumps, build scaffolding, project history)
  - [x] Whitelist-based file pre-filtering (replaces blacklist approach):
    - [x] `ALLOWED_EXTENSIONS`: 14 config extensions (`.toml`, `.yaml`, `.yml`, `.json`, `.jsonc`, `.ini`, `.cfg`, `.conf`, `.config`, `.xml`, `.properties`, `.env`, `.rc`, `.plist`)
    - [x] `ALLOWED_NAMED_FILES`: extensionless names (`config`, `credentials`)
    - [x] `HOME_BLOCKED_DOTFILES`: ~18 known noise dotfiles rejected at `$HOME` root
    - [x] Home-root dotfiles accepted if extensionless or allowed extension
  - [x] `PRUNE_DIRS` expanded with `registry`, `bin`, `extensions`, `file-history`, `backups`, `todos`, `plugins`, `themes`, `custom`, `l10n`/`locales`/`locale`, `licenses`, `projects`, `tasks`, `conversations`, `events`, `subagents`, `language`, `gitstatus`, `.github`, `.gitlab`
  - [x] `_should_prune_dir()` excludes `repo_path` by resolved path comparison (dynamic, name-independent)
  - [x] 103 tests + 2 perf tests with full acceptance criteria coverage
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
  - [x] `register_new_files()` — new file registration (propagates `ConfigFile.sensitive` → `ManifestEntry.sensitive_flagged`)
  - [x] `Conflict` / `detect_conflicts()` — mtime-based conflict detection
  - [x] 26 tests with full coverage

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

- [x] Orchestration layer (`orchestrator.py`)
  - [x] `run_discover()` — scan → enforce NEVER_INCLUDE → resolve pending → flag → confirm sensitive → register
  - [x] `run_sync()` — load manifest → flag → snapshot → plan → execute → commit/push → health checks
  - [x] `run_restore()` — pull → snapshot → plan → execute → health checks (or direct snapshot rollback)
  - [x] `DiscoverResult`, `SyncResult`, `RestoreResult` dataclasses for structured CLI rendering
  - [x] `_manifest_to_config_files()`, `_resolve_sensitive_confirmations()`, `_mark_sensitive()` helpers
  - [x] No Typer/Rich imports; interaction callbacks injected by caller
  - [x] `tests/test_orchestrator.py` — unit tests for all helpers and workflow functions

- [x] CLI interface (`main.py`)
  - [x] `init` command with `--repo-path`, `--remote`, `--llm-endpoint`
  - [x] `discover` command with `--no-ai` and interactive resolution; AI debug details via `--verbose`
  - [x] `_check_llm_connectivity()` — probes LLM endpoint before discover, warns and prompts to continue/abort
  - [x] `sync` command with full pipeline orchestration
  - [x] `restore` command with pull + restore or direct snapshot rollback
  - [x] `rollback` command with interactive selection and integrity verification
  - [x] `status` command with config/repo/snapshot summary
  - [x] `config` command with `--show` and `--set KEY=VALUE`
  - [x] Structured exit codes (0–5)
  - [x] Tests updated to cover LLM connectivity pre-check

- [x] Rich UI helpers (`ui.py`)
  - [x] `print_success()`, `print_warning()`, `print_error()`, `print_section()`
  - [x] `file_table()`, `snapshot_table()`, `flag_panel()`
  - [x] `ScanStats` dataclass (with `start_time`) and `make_scan_display()` returning `Group` with spinner + stats table
  - [x] Animated `Spinner("dots")` and elapsed time counter — auto-refreshes between events
  - [x] Live progress wired into `discover` and `sync` commands via `_run_discover_with_progress()`
  - [x] `--verbose` surfaces `dir_pruned`/`file_rejected` events at DEBUG level
  - [x] `--verbose` logs full list of accepted files after scan phase ends
  - [x] `--verbose` logs AI batch details (paths, timing, verdicts, errors) at DEBUG level
  - [x] All debug output unified through Python `logging` → `~/.dotsync/dotsync.log` (always) + console (with `--verbose`)
  - [x] 11 tests covering CLI (up from 7)

- [x] Integration tests (`test_integration.py`)
  - [x] Shared test fixtures in `tests/conftest.py`: `dotsync_env`, `sample_dotfiles`, `mock_gitcrypt`, `mock_health_checks`
  - [x] Config → Discovery pipeline (3 tests): home dotfile discovery, exclude patterns, repo_path exclusion
  - [x] Discovery → Flagging → Registration (3 tests): secret detection, clean file pass-through, sensitive flag propagation
  - [x] Sync pipeline (3 tests): file copy to repo, manifest update, dry-run no-op
  - [x] Restore pipeline (2 tests): repo → home restore, cross-platform path transformation
  - [x] Snapshot → Rollback (2 tests): create/modify/rollback cycle, retention limit enforcement
  - [x] Health → Auto-rollback (2 tests): post-operation pass, failure-triggered rollback
  - [x] 15 integration tests total

- [x] End-to-end CLI tests (`test_e2e.py`)
  - [x] `init` creates config and repo, idempotent on second run
  - [x] `status` shows repo path and managed file count
  - [x] `config --show` and `config --set` round-trip
  - [x] `discover --no-ai` finds and lists dotfiles
  - [x] `sync --dry-run --no-push` reports dry run without copying files
  - [x] 6 e2e tests total via Typer `CliRunner`

- [x] Manual smoke testing of full CLI workflows
  - [x] `init` → `status` → `config --show` → `config --set` → `config --show` (verify)
  - [x] `discover --no-ai` with interactive confirmation
  - [x] `sync --dry-run --no-push` shows plan without side effects
  - [x] `sync --no-push` copies files, commits locally, passes health checks
  - [x] `restore --no-pull` restores files from repo, content matches original
  - [x] `rollback --list` shows snapshots with correct triggers and retention
  - [x] `init` idempotent (second run with overwrite confirmation)

- [x] LLM client (`llm_client.py`)
  - [x] `probe_llm()` — minimal pre-flight request returning `(bool, reason)` with error classification
  - [x] `_base_url()`, `_chat_url()`, `_models_url()` — URL normalisation helpers
  - [x] `llm_api_key` field in `DotSyncConfig` with `{env:VAR}` substitution; threaded through `chat_completion()`, flagging, and discovery

- [x] Test infrastructure
  - [x] `pytest.mark.integration` and `pytest.mark.e2e` markers in `pyproject.toml`
  - [x] 330 tests total (328 collected, 2 perf deselected)

- [x] README.md with user-facing documentation
  - [x] Features, quick start, command reference, configuration, security, AI triage, development, and project structure sections
  - [x] `llm_api_key` added to configuration table
  - [x] `orchestrator.py` added to project structure

### In Progress
- (none)

### Pending
- (none)

## Next Steps

All core modules, CLI commands, and test suites are complete. Potential future work:
- ~~Cross-platform testing on Windows~~ (initial Windows fixes applied: health checks, git push)
- CI pipeline setup
- Package publishing
