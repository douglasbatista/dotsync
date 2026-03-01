# DotSync

CLI tool to backup, sync, and restore configuration files (dotfiles) across Windows and Linux workstations, with git-crypt encryption and optional AI-powered file classification.

## Features

- **Encrypted storage** -- git-crypt symmetric encryption for all synced files
- **Cross-platform sync** -- Linux and Windows with automatic path transformation
- **AI-powered classification** -- optional LLM triage to decide which files to include
- **Local snapshots** -- timestamped backups with automatic rollback on health check failure
- **Sensitive data flagging** -- regex scanning and interactive confirmation before committing
- **Health checks** -- configurable post-operation shell commands to verify system integrity

## Quick Start

```bash
# Install
uv sync

# Initialize config and repository
uv run dotsync init --repo-path ~/dotsync-repo --remote git@github.com:user/dotfiles.git

# Discover and classify config files
uv run dotsync discover

# Sync files to the repository
uv run dotsync sync

# Restore files on another machine
uv run dotsync restore
```

## Commands

| Command | Description | Key Flags |
|---------|-------------|-----------|
| `init` | Initialize configuration, repository, and git-crypt | `--repo-path`, `--remote`, `--llm-endpoint` |
| `discover` | Scan, classify, and register config files into the manifest | `--no-ai` |
| `sync` | Copy managed files to the repo, commit, and push | `--dry-run`, `--no-push`, `--message` |
| `restore` | Pull from remote and restore files to the home directory | `--dry-run`, `--no-pull`, `--from-snapshot` |
| `rollback` | Revert to a previous snapshot | `--dry-run`, `--list` |
| `status` | Show config summary, managed file count, and snapshot count | |
| `config` | View or modify DotSync configuration | `--show`, `--set KEY=VALUE` |

All commands support the global `--verbose` / `-v` flag for debug logging.

## Configuration

Configuration is stored at `~/.dotsync/config.toml`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `repo_path` | path | `~/dotsync-repo` | Git repository for storing dotfiles |
| `remote_url` | string | | Remote Git URL |
| `gitcrypt_key_path` | path | | Path to git-crypt symmetric key |
| `llm_endpoint` | string | | LiteLLM proxy endpoint for AI triage |
| `llm_model` | string | `claude-haiku-4-5` | LLM model name |
| `snapshot_keep` | int | `5` | Number of local snapshots to retain |
| `health_checks` | list | `[]` | Post-operation shell commands |
| `exclude_patterns` | list | `[]` | Glob patterns to exclude from sync |
| `include_extra` | list | `[]` | Additional paths to include |

Update a value:

```bash
uv run dotsync config --set llm_endpoint=http://localhost:4000
```

## Security

DotSync uses git-crypt symmetric encryption. All files committed to the repository are encrypted at rest; they are only readable after unlocking with the key.

Additional safeguards:

- **SENSITIVE_PATTERNS** -- 11 compiled regexes scan file contents for secrets (API keys, PEM blocks, connection strings, tokens)
- **NEVER_INCLUDE blocklist** -- `.ssh/id_rsa`, `.ssh/id_ed25519`, `.ssh/id_ecdsa`, `.gnupg/`, and `dotsync_key` are unconditionally excluded
- **SAFETY_EXCLUDES** -- SSH keys, `.gnupg/`, `.dotsync/`, and the encryption key are blocked at the discovery layer before classification
- **Interactive confirmation** -- files flagged as sensitive prompt for `[I]nclude / [E]xclude / [S]kip` before syncing

## AI Triage (Optional)

Enable AI-powered file classification by setting an LLM endpoint:

```bash
uv run dotsync init --llm-endpoint http://localhost:4000
```

Compatible with any OpenAI-compatible API:

- **LiteLLM** proxy
- **OpenRouter**
- **Ollama**

The LLM classifies ambiguous files as include, exclude, or ask_user based on whether the file reflects user choices (plugins, themes, keybindings) or is infrastructure a tool would regenerate on reinstall. Results are cached locally. Skip AI for a single run with `--no-ai`:

```bash
uv run dotsync discover --no-ai
```

## Development

### Prerequisites

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- git
- git-crypt

### Setup

```bash
uv sync
source .venv/bin/activate
```

### Tests

```bash
uv run pytest
uv run pytest --cov=dotsync --cov-report=term-missing
```

### Lint and type check

```bash
uv run ruff check .
uv run ruff format .
uv run mypy src/
```

## Project Structure

```
src/dotsync/
  main.py           # Typer CLI entry point and command definitions
  config.py          # TOML config schema (Pydantic), load/save
  discovery.py       # File scanner, heuristic + AI classification
  flagging.py        # Sensitive data regex scanning and AI flagging
  git_ops.py         # Git repository and git-crypt subprocess operations
  sync.py            # Sync (home -> repo) and restore (repo -> home) engine
  snapshot.py        # Local timestamped backups and rollback
  health.py          # Post-operation health checks with auto-rollback
  llm_client.py      # httpx client for OpenAI-compatible LLM endpoints
  platform_utils.py  # OS detection and home directory resolution
  logging_setup.py   # Logging configuration
  ui.py              # Rich terminal output helpers and tables
```
