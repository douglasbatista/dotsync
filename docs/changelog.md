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
- None

### Fixed
- None
