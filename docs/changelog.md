# Changelog

## [Unreleased]

### Added
- `orchestrator.py` — pure business-logic layer for discover, sync, and restore workflows (P0.2). Contains `run_discover()`, `run_sync()`, and `run_restore()` returning structured result dataclasses (`DiscoverResult`, `SyncResult`, `RestoreResult`). No Typer/Rich imports; interaction callbacks injected by the CLI layer.
- `test_orchestrator.py` — unit tests for the orchestration layer covering helpers (`_manifest_to_config_files`, `_resolve_sensitive_confirmations`, `_mark_sensitive`) and all three workflow functions with mocked dependencies.
- `probe_llm()` in `llm_client.py` — sends a minimal chat-completion request before AI triage begins, returns `(bool, reason)` tuple. Failure reason distinguishes auth errors, wrong model, connection refused, and timeout.
- `_check_llm_connectivity()` in `main.py` — runs `probe_llm()` before discover, warns with the reason if unreachable, and prompts to continue without AI or abort. Avoids waiting through all batches to discover a bad endpoint.
- `llm_api_key` field in `DotSyncConfig` — optional bearer token for the LLM endpoint. Supports `{env:VAR}` substitution so secrets stay out of the config file. Threaded through `chat_completion()`, flagging, and discovery.
- AI batch progress counter in scan display — shows `done/total` batches alongside the existing scan stats.
- URL normalisation helpers in `llm_client.py`: `_base_url()`, `_chat_url()`, `_models_url()`.

## [0.2.0] - 2026-04-20

### Added
- User manual (`docs/user_manual.md`) — full end-user guide covering prerequisites, first-time setup, second-machine setup, all seven commands with option tables, configuration reference, security model, AI triage, health checks, snapshots/rollback, cross-platform sync, and troubleshooting
- README updated with link to user manual, corrected `gitcrypt_key_path` default, condensed git-crypt install table, and `dotsync.log` note

### Fixed
- `dotsync_key` renamed to `dotsync.key` in `NEVER_INCLUDE` blocklist — matches the actual key filename written by `init_gitcrypt()`
- Email regex in `SENSITIVE_PATTERNS` simplified (`[A-Z|a-z]` → `[A-Za-z]`) to remove spurious `|` in character class
- Shell health check now resolves `$SHELL` at runtime (`os.environ.get("SHELL", "/bin/sh")`) instead of passing the literal string `${SHELL}` as the executable — fixes false failures when `$SHELL` is not expanded by the subprocess runner
- Health check name for user-configured commands now uses only the executable basename (e.g. `zsh` instead of `/usr/bin/zsh`) for cleaner output
- `check_and_rollback_if_needed()` now catches rollback exceptions and reports failure clearly instead of raising an unrelated error
- `scan_candidates()` now catches exceptions from individual scan root futures — a broken root no longer aborts the entire scan
- Snapshot IDs include milliseconds (`%Y-%m-%dT%H-%M-%S-NNN`) to prevent collisions when two snapshots are created within the same second
- `verify_snapshot()` uses `Path.as_posix()` for relative path comparison — fixes false mismatch on Windows where `str(Path)` uses backslashes
- `execute_restore()` guards against `None` transform parameters with a safe fallback copy instead of `assert` statements
- `datetime.now()` calls in `sync.py` and `snapshot.py` are now timezone-aware (`tz=timezone.utc`) — eliminates `DeprecationWarning` and ensures consistent behaviour across platforms
- `filter_by_profile()` simplified to use `in` membership test

## [Unreleased] — development history

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

### Changed (continued)
- Discovery module: two-phase filtering refactor with performance-first scanner
  - Replaced `os.walk()` with `os.scandir()` + manual recursion via `_scan_dir()` for efficient `DirEntry` stat reuse
  - Parallel root scanning via `ThreadPoolExecutor` (one thread per scan root)
  - Replaced `SCAN_EXCLUDES` with `PRUNE_DIRS` (~33 directory names, matched by name) + `_PRUNE_PREFIXES` (`.local/share/`, `.local/lib/`)
  - Added `BLOCKED_EXTENSIONS` (~48 file extensions) and `BLOCKED_FILENAMES` (7 exact names + glob patterns) for file-level pre-filtering
  - Added `_prefilter_file()` consolidating all file-level checks ordered cheapest-first
  - Reduced `MAX_FILE_SIZE` from 512 KB to 50 KB, `BINARY_CHECK_BYTES` from 8192 to 512
  - Safety excludes now prune at directory level in addition to file level
  - `PermissionError` on inaccessible directories silently skipped
  - Extra paths bypass prune dirs and blocked lists but not safety excludes (unchanged behavior)
  - Added `ScanEvent` TypedDict, `ProgressCallback` type alias, and `_emit()` helper for real-time progress reporting
  - `scan_candidates()`, `classify_with_ai()`, and `discover()` accept optional `progress` callback
  - Scanner emits `root_start`/`root_done`, `dir_enter`/`dir_pruned`, `file_accepted`/`file_rejected` events
  - `discover()` emits `phase_start`/`phase_done` events for scan, heuristic, and ai_triage phases
  - `classify_with_ai()` emits `ai_batch` events per batch
  - Test suite expanded from 33 to 57 tests in `test_discovery.py`

### Added (continued)
- Live scan progress display in CLI
  - `ScanStats` dataclass and `make_scan_display()` Rich table helper in `ui.py`
  - `_run_discover_with_progress()` helper in `main.py` using `Rich.Live` for real-time scan statistics
  - `discover` and `sync` commands now show live progress (dirs scanned, files accepted/rejected, current directory, phase)
  - Animated spinner (`Spinner("dots")`) and elapsed time counter in scan display — auto-refreshes via `Rich.Live` so the UI always shows activity, even during long AI API calls
  - `--verbose` flag now surfaces `dir_pruned` and `file_rejected` events at DEBUG level
  - `--verbose` logs the full list of accepted files after scan phase completes
  - 4 new tests covering progress callback wiring and verbose logging

### Added (continued)
- Generated filename detection in discovery scanner
  - `BLOCKED_FILENAME_PATTERNS`: compiled regexes for UUID, hex (16+ chars), numeric, and hex-with-dots filenames
  - `_is_generated_filename()` function wired into `_prefilter_file()` (runs after blocked extension/filename, before size check)
  - `.md` and `.rst` added to `BLOCKED_EXTENSIONS` (documentation files are never config)
  - `@pytest.mark.perf` marker registered in `pyproject.toml`, excluded from default test runs

### Changed (continued)
- Discovery constants expanded for broader noise filtering
  - `PRUNE_DIRS` expanded: `registry` (Cargo), `bin`/`extensions` (VS Code server), `file-history`/`backups`/`todos` (app state), `plugins` (shell plugin code), `l10n`/`locales`/`locale` (i18n), `licenses`
  - `BLOCKED_EXTENSIONS` expanded: `.sh`/`.bash` (shell scripts), `.txt` (plain text), `.orig`/`.bak`/`.backup`/`.tmp` (backup/temp files)
  - `BLOCKED_FILENAMES` expanded: `.cargo-ok`/`.cargo_vcs_info.json` (Cargo metadata), `LICENSE`/`LICENSE-MIT`/`LICENSE-APACHE`/`LICENSE-BSD`/`COPYING`/`NOTICE`/`AUTHORS`/`CONTRIBUTORS` (license/legal), `README`/`CHANGELOG`/`CHANGES`/`HISTORY` (docs)
  - `BLOCKED_FILENAME_PATTERNS` updated: dot-prefixed UUID support, hex with `@version` suffix, trailing Unix timestamp (`\.\d{10,}$`)
  - `_is_generated_filename()` now checks both `stem` and `name` (was stem only) using `search()` (was `match()`)
  - Test suite expanded from 69 to 86 tests in `test_discovery.py` (+ 2 perf tests)

### Changed (continued)
- AI system prompt rewritten with **environment vs infrastructure** framing (replaces ownership-based version)
  - Old question: "Would this user want to replicate this file on another machine?"
  - New question: "Is this file part of the user's computing environment, or internal infrastructure that the tool recreates on reinstall?"
  - Removes authorship as a criterion — default configs and pinned versions count as environment
  - INCLUDE trigger: "reflects a choice" — installed plugins, selected theme, registry mirror, auth context
  - EXCLUDE trigger: "reinstalling the tool would produce this file with identical content"
  - Credentials/private keys explicitly declared out of scope (handled separately by safety excludes)

### Changed (continued)
- `_should_prune_dir()` now prunes directories with generated names (UUID, hex, numeric) via `_is_generated_filename()`
  - Prevents descent into directories like `.f9c91a88-3095-44a3-bbb5-011673bd7cc9/`
  - Reuses existing `_is_generated_filename()` and `BLOCKED_FILENAME_PATTERNS` — no new regexes
  - Test suite expanded from 108 to 110 tests in `test_discovery.py` (+ 2 perf tests)

### Changed (continued)
- `$HOME` shallow scan architecture
  - `platform_utils.config_dirs()` now returns `list[tuple[Path, int]]` — each root has its own max scan depth
  - `HOME_SCAN_DEPTH = 1` limits `$HOME` to direct children only (dotfiles), preventing descent into user repos/projects
  - `KNOWN_CONFIG_SUBDIRS` expanded to 21 entries: XDG (`.config`, `.local`), shell (`.oh-my-zsh`, `.zsh`, `.bash_it`), editors (`.vim`, `.nvim`, `.emacs.d`, `.nano`), dev tools (`.ssh`, `.gnupg`, `.aws`, `.kube`, `.docker`, `.cargo`, `.rustup`, `.npm`, `.nvm`, `.pyenv`, `.rbenv`), dotfiles repo (`.git`)
  - Known subdirs only added as roots if they exist on disk
  - `XDG_CONFIG_HOME` respected when set and different from `~/.config`
  - Windows AppData roots use depth 4 (matching heuristic rule max_depth)
  - `scan_candidates()` updated to unpack `(Path, max_depth)` tuples from `config_dirs()`
- Discovery constants expanded for deeper noise filtering
  - `PRUNE_DIRS` expanded: `themes`/`custom` (shell themes), `projects`/`tasks`/`conversations`/`events`/`subagents` (AI agent state), `language`/`gitstatus` (shell prompt internals), `.github`/`.gitlab` (repository metadata)
  - `BLOCKED_EXTENSIONS` expanded: `.jsonl` (structured logs), `.po`/`.pot` (gettext), `.zsh-theme`/`.theme` (shell themes), `.info` (metadata)
  - `BLOCKED_FILENAMES` rewritten: removed `*.log`/`*.pid` glob patterns (handled by BLOCKED_EXTENSIONS), added `.lock`/`.highwatermark`/`.pid` (runtime state markers), `Makefile`/`makefile`/`GNUmakefile`/`build.info`/`bindgen` (build files)
  - Removed `_BLOCKED_FILENAME_GLOBS` mechanism (no longer needed — all entries are exact matches)
  - Test suite expanded from 86 to 108 tests in `test_discovery.py` (+ 2 perf tests)

### Added (continued)
- Sensitivity persistence pipeline
  - `ConfigFile.sensitive: bool = False` field added to discovery model (Module 00 spec alignment)
  - `_mark_sensitive()` helper in `main.py` — sets `sensitive=True` on ConfigFiles with regex matches or AI flags that were confirmed for inclusion
  - Called after `confirm_sensitive_files()` in both `discover` and `sync` commands
  - `register_new_files()` now propagates `ConfigFile.sensitive` → `ManifestEntry.sensitive_flagged` (was hardcoded `False`)
  - `_manifest_to_config_files()` propagates `ManifestEntry.sensitive_flagged` → `ConfigFile.sensitive` for round-trip consistency
  - 6 new tests: 4 for `_mark_sensitive()` (match, AI-flagged, unconfirmed skip, clean file), 2 for `register_new_files()` sensitive propagation

### Changed (continued)
- Configuration path expansion (Module 01 spec alignment)
  - Added `expand_path(p, resolve)` utility function to `config.py` — expands `~`, `$HOME`, `%USERPROFILE%` via `os.path.expandvars` + `Path.expanduser()`, optionally resolves to absolute
  - Added Pydantic `field_validator` decorators on `DotSyncConfig`:
    - `repo_path`, `gitcrypt_key_path` — full expansion + resolve
    - `include_extra` — full expansion + resolve for each path in list
    - `exclude_patterns` — expanduser/expandvars only, no resolve (patterns may contain globs)
    - `health_checks` — intentionally left unvalidated (shell handles `~` at runtime)
  - `include_extra` field type changed from `list[str]` to `list[Path]` (spec alignment)
  - `config --set` handler coerces list values to `Path` when field type contains `Path`
  - `expand_path` exported from `dotsync.__init__`
  - 11 new tests in `test_core.py`: `TestExpandPath` (4 tests) and `TestConfigPathExpansion` (7 tests)
- Discovery scanner now accepts `repo_path` parameter (Module 02 spec alignment)
  - `scan_candidates(repo_path=...)` threads repo path through `_scan_dir()` and `_should_prune_dir()`
  - `_should_prune_dir()` excludes directories matching `repo_path` by resolved path comparison — works regardless of directory name
  - `discover()` passes `cfg.repo_path` to `scan_candidates()`
  - 2 new tests: `test_prune_skips_repo_path`, `test_prune_skips_repo_path_custom_name`

### Added (continued)
- Integration and end-to-end test suites
  - `tests/conftest.py` — shared fixtures: `dotsync_env` (full filesystem isolation via `tmp_path`), `sample_dotfiles`, `mock_gitcrypt`, `mock_health_checks`
  - `tests/test_integration.py` — 15 cross-module integration tests covering config→discovery, discovery→flagging→registration, sync, restore, snapshot→rollback, and health→auto-rollback pipelines
  - `tests/test_e2e.py` — 6 end-to-end CLI tests via Typer `CliRunner` covering `init`, `status`, `config`, `discover`, and `sync --dry-run`
  - `pytest.mark.integration` and `pytest.mark.e2e` markers registered in `pyproject.toml`
  - Manual smoke testing of all CLI workflows: init, status, config, discover, sync, restore, rollback
  - Total test count: 277 (254 unit + 15 integration + 6 e2e + 2 perf deselected)

### Added (continued)
- README.md with user-facing project documentation
  - Features overview, quick start guide, CLI command reference table
  - Configuration field reference and `--set` usage example
  - Security model documentation (git-crypt, SENSITIVE_PATTERNS, NEVER_INCLUDE, SAFETY_EXCLUDES, interactive I/E/S)
  - AI triage setup (LiteLLM, OpenRouter, Ollama compatibility)
  - Development prerequisites and setup commands
  - Project structure tree with module descriptions

### Changed (continued)
- Discovery module: switched from blacklist to whitelist-based file pre-filtering
  - Removed `BLOCKED_EXTENSIONS` (~48 extensions), `BLOCKED_FILENAMES` (~30 names), and `_is_blocked_file()` function
  - Added `ALLOWED_EXTENSIONS` (14 config extensions: `.toml`, `.yaml`, `.yml`, `.json`, `.jsonc`, `.ini`, `.cfg`, `.conf`, `.config`, `.xml`, `.properties`, `.env`, `.rc`, `.plist`)
  - Added `ALLOWED_NAMED_FILES` (extensionless names: `config`, `credentials`)
  - Added `HOME_BLOCKED_DOTFILES` (~18 known noise dotfiles: history files, auth tokens, session errors)
  - `_prefilter_file()` rewritten with whitelist logic: safety → size → whitelist gate → binary check
  - Home-root dotfiles (direct `$HOME` children starting with `.`) get special handling: accepted if extensionless or allowed extension, rejected if in `HOME_BLOCKED_DOTFILES`
  - `_scan_dir()` now passes `is_home_root` flag to `_prefilter_file()` for context-aware filtering
  - `_is_generated_filename()` removed from file-level filtering (retained in `_should_prune_dir()` for directory pruning only)
  - Test suite rewritten: blacklist-specific tests replaced with whitelist accept/reject tests; 94 tests (+ 2 perf deselected)
  - Total test count: 270 (+ 2 perf deselected)

### Changed (continued)
- AI triage pipeline reliability improvements
  - `chat_completion()` now retries on transient errors (timeout, HTTP) with exponential backoff (2s, 4s); `max_retries=2` default; malformed responses raise immediately without retry
  - Default HTTP timeout increased from 15s to 90s
  - `MAX_CANDIDATES_PER_BATCH` reduced from 20 to 10 for faster per-batch responses
  - Failed AI batch no longer abandons all remaining batches — only the failed batch is marked `ai:unreachable`, processing continues to the next batch
  - AI system prompt expanded with EXCLUDE rules for: server-pushed feature flags, addon default settings vs user overrides, IDE internal storage (globalStorage, argv.json), OEM/vendor bloatware, VPN auto-generated settings dumps, build/packaging scaffolding (package.json, tsconfig.json in tool dirs), project/file history; INCLUDE section adds caveat that settings.json requires content/path inspection
  - `BLOCKED_FILENAME_PATTERNS` expanded: pure hex threshold lowered from 16 to 8 chars; new pattern for embedded hex 32+ chars (SHA-256 in filenames)
  - Heuristic classifier no longer auto-includes files — tags reason but leaves `include=None` for AI to decide; falls back to `include=True` when no AI endpoint is configured
  - Removed `--debug` flag and `ai_debug.jsonl` mechanism; AI triage details now logged via standard Python `logging` at DEBUG level, visible with `--verbose` on console and always written to `~/.dotsync/dotsync.log`
  - 10 new tests for retry behaviour, continue-on-failure, heuristic fallback; test suite updated for new heuristic semantics
  - Test count: 103 in `test_discovery.py` + 10 in `test_llm_client.py`

### Fixed
- `register_new_files()` no longer hardcodes `sensitive_flagged=False` — sensitivity detection results are now persisted in the manifest
- `commit_and_push()` now specifies branch refspec and `--set-upstream` when pushing — fixes `fatal: The current branch has no upstream branch` on first push
- `commit_and_push()` now checks `PushInfo.ERROR` flags and raises `GitCommandError` on push failure — previously errors like "push declined due to email privacy restrictions" were silently swallowed
- Push failures in `sync` command now show a warning instead of an unhandled traceback — commit is preserved locally
- Shell health check now uses `cmd /c echo ok` on Windows instead of `${SHELL} -c 'echo ok'` — fixes false failure and unnecessary rollback on Windows
