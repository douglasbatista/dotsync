# Project Status

## Current Milestone: Git & git-crypt Integration Complete ✅

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

### In Progress
- [ ] Sync engine implementation

### Pending
- [ ] Snapshot management
- [ ] Health check integration
- [ ] UI/progress indicators
- [ ] `sync` command implementation
- [ ] `restore` command implementation
- [ ] `rollback` command implementation
- [ ] `status` command implementation
- [ ] Integration tests
- [ ] End-to-end testing

## Next Steps

1. Implement `sync.py` - core sync logic
2. Implement `snapshot.py` - local snapshot management
3. Wire up CLI commands (`sync`, `restore`, `status`)
