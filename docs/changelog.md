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

### Fixed
- None
