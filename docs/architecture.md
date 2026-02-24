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

- Uses GitPython for standard Git operations
- Calls git-crypt via subprocess for encryption
- Manages the dotfiles repository

### 6. Sync Engine (`sync.py`)

- Core sync logic
- Copies files between home directory and repository
- Handles conflicts and versioning

### 7. Snapshots (`snapshot.py`)

- Creates local snapshots before sync operations
- Manages snapshot retention based on `snapshot_keep` config
- Snapshots are local-only, never committed

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
