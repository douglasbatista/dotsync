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

- Scans home directory for dotfiles
- Applies exclude patterns and include extras
- Returns list of files to sync

### 4. Flagging (`flagging.py`)

- Tracks which files have been modified
- Maintains state between sync operations

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
