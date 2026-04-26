# DotSync User Manual

## Table of Contents

1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Installation](#installation)
4. [First-Time Setup](#first-time-setup)
5. [Daily Workflow](#daily-workflow)
6. [Command Reference](#command-reference)
7. [Configuration Reference](#configuration-reference)
8. [Security Model](#security-model)
9. [AI Triage](#ai-triage)
10. [Health Checks](#health-checks)
11. [Snapshots and Rollback](#snapshots-and-rollback)
12. [Cross-Platform Sync](#cross-platform-sync)
13. [Troubleshooting](#troubleshooting)

---

## Overview

DotSync backs up and syncs your configuration files (dotfiles) across machines using a Git repository. Files are stored in plain text — push to a **private** remote to keep your dotfiles safe.

Key capabilities:

- **Cross-platform sync** — handles Linux ↔ Windows path differences automatically
- **Smart discovery** — heuristic scanner with optional AI classification decides what to include
- **Sensitive-data protection** — regex and AI scanning flags secrets before they reach the repo
- **Local snapshots** — automatic pre-operation backups with one-command rollback
- **Health checks** — configurable post-operation shell commands with auto-rollback on failure

---

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Python | 3.12.x | Required |
| [uv](https://docs.astral.sh/uv/) | latest | Package manager |
| git | any | Must be on PATH |

---

## Installation

```bash
git clone <this-repo>
cd dotsync
uv sync
```

Verify the CLI is working:

```bash
uv run dotsync --help
```

---

## First-Time Setup

Run `init` once per machine to create your configuration and initialize the dotfiles repository.

```bash
uv run dotsync init \
  --repo-path ~/dotsync-repo \
  --remote git@github.com:you/dotfiles.git
```

What `init` does:

1. Creates `~/.dotsync/config.toml` with your settings
2. Creates (or opens) the Git repo at `--repo-path`
3. Writes `.gitattributes` and an empty manifest
4. Sets the remote origin (if `--remote` is provided)

> **Use a private remote.** Dotfiles are stored in plain text. Push only to private repositories to protect any sensitive values in your config files.

### Setting up a second machine

On a new machine, clone the repository and run restore:

```bash
git clone git@github.com:you/dotfiles.git ~/dotsync-repo
uv run dotsync init --repo-path ~/dotsync-repo
uv run dotsync restore
```

---

## Daily Workflow

### First use on a machine

```bash
# 1. Scan and classify your config files
uv run dotsync discover

# 2. Sync them to the repo (creates a snapshot first)
uv run dotsync sync
```

### Regular sync (after changing config files)

```bash
uv run dotsync sync
```

### Pull latest on another machine

```bash
uv run dotsync restore
```

### Check what's managed

```bash
uv run dotsync status
```

---

## Command Reference

All commands accept `--verbose` / `-v` for debug output (also written to `~/.dotsync/dotsync.log`).

---

### `init`

Initialize DotSync configuration and the dotfiles repository.

```bash
uv run dotsync init [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--repo-path PATH` | Path for the dotfiles Git repository (default: `~/dotsync-repo`) |
| `--remote TEXT` | Remote Git URL to push to |
| `--llm-endpoint TEXT` | LiteLLM / OpenAI-compatible endpoint for AI triage |

If a config already exists, you are prompted to confirm before overwriting.

---

### `discover`

Scan the home directory, classify config files, and interactively resolve ambiguous results.

```bash
uv run dotsync discover [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--no-ai` | Skip AI classification; use heuristics only |

The scanner walks your home directory and evaluates each file against:

1. **Safety excludes** — SSH private keys, `.gnupg/`, `.dotsync/` — always blocked
2. **Extension whitelist** — only `.toml`, `.yaml`, `.yml`, `.json`, `.ini`, `.cfg`, `.conf`, `.env`, `.rc`, `.plist`, `.xml`, `.properties`, `.jsonc`, `.config` pass
3. **Heuristic rules** — home dotfiles, XDG config paths, Windows AppData paths
4. **AI classification** (optional) — LLM decides include / exclude / ask_user

After classification, files marked `ask_user` are shown for interactive confirmation.

Discovery results are saved to the manifest (`~/.dotsync_manifest.json` inside the repo). You can re-run `discover` at any time to pick up new files.

---

### `sync`

Copy managed files to the repository, commit, and push.

```bash
uv run dotsync sync [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--dry-run` | Show what would happen without writing anything |
| `--no-push` | Commit locally but do not push to remote |
| `--message TEXT` | Custom commit message |

**Full sync pipeline:**

1. Run discovery (same as `discover`)
2. Scan included files for secrets (`flagging`)
3. Prompt for confirmation on flagged files
4. Create a local snapshot
5. Copy files to the repository
6. Commit and push
7. Run health checks — auto-rollback if any fail

---

### `restore`

Pull from remote and restore files to the home directory.

```bash
uv run dotsync restore [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--dry-run` | Show what would be restored without writing anything |
| `--no-pull` | Restore from local repo state without pulling |
| `--from-snapshot SNAPSHOT_ID` | Restore from a specific local snapshot instead |

**Restore pipeline:**

1. Pull latest from remote (unless `--no-pull`)
2. Create a local snapshot of current files
3. Copy repo files to home directory
4. Run health checks — auto-rollback if any fail

---

### `rollback`

Revert your home directory to a previous snapshot.

```bash
uv run dotsync rollback [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--list` | List available snapshots and exit |
| `--dry-run` | Show what would be restored without writing |

Without options, presents an interactive menu of available snapshots with timestamps, triggers, and file counts. Selecting one restores those files and verifies integrity.

---

### `status`

Show a summary of the current DotSync state.

```bash
uv run dotsync status
```

Displays:

- Configuration path and key settings
- Repository path and remote URL
- Number of managed files
- Number of available snapshots
- Git status of the repository

---

### `config`

View or modify DotSync configuration.

```bash
# Show all current settings
uv run dotsync config --show

# Update a single setting
uv run dotsync config --set KEY=VALUE
```

Valid keys: `repo_path`, `remote_url`, `llm_endpoint`, `llm_api_key`, `llm_model`, `snapshot_keep`.

Example:

```bash
uv run dotsync config --set llm_endpoint=http://localhost:4000
uv run dotsync config --set llm_api_key={env:OPENROUTER_API_KEY}
uv run dotsync config --set snapshot_keep=10
```

---

## Configuration Reference

Configuration is stored at `~/.dotsync/config.toml`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `repo_path` | path | `~/dotsync-repo` | Git repository for storing dotfiles |
| `remote_url` | string | | Remote Git URL |
| `llm_endpoint` | string | | LiteLLM / OpenAI-compatible endpoint for AI triage |
| `llm_api_key` | string | | Bearer token for the LLM endpoint (supports `{env:VAR}` substitution) |
| `llm_model` | string | `claude-haiku-4-5` | LLM model name |
| `snapshot_keep` | int | `5` | Number of local snapshots to retain (`0` = keep all) |
| `health_checks` | list | `[]` | Additional post-operation shell commands |
| `exclude_patterns` | list | `[]` | Glob patterns to exclude from discovery |
| `include_extra` | list | `[]` | Additional file paths to always include |

### Example config

```toml
repo_path = "/home/alice/dotsync-repo"
remote_url = "git@github.com:alice/dotfiles.git"
llm_endpoint = "http://localhost:4000"
llm_api_key = "{env:OPENROUTER_API_KEY}"
llm_model = "claude-haiku-4-5"
snapshot_keep = 5

exclude_patterns = ["**/.cache/**", "**/node_modules/**"]
include_extra = ["/home/alice/.config/custom-tool/config.json"]

[[health_checks]]
name = "shell ok"
command = "bash -c 'echo ok'"
timeout = 10
```

---

## Security Model

### Multi-layer secret protection

DotSync has three independent layers that prevent secrets from reaching the repository:

1. **Safety excludes** (discovery layer) — SSH private keys (`.ssh/id_rsa`, `.ssh/id_ed25519`, `.ssh/id_ecdsa`), `.gnupg/`, `.dotsync/` are blocked before any classification runs. Also enforced on `include_extra` paths.

2. **Regex scanning** (flagging layer) — eleven compiled patterns scan file contents for:
   - GitHub tokens (`ghp_`, `github_pat_`)
   - AWS access/secret keys
   - OpenAI and Anthropic API keys
   - PEM blocks
   - Connection strings (postgres, mysql, mongodb, redis)
   - Generic `token =` / `api_key =` patterns
   - Email addresses

3. **AI flag check** (flagging layer) — when no regex matches are found, an optional LLM reviews the first 30 lines of each file and assesses sensitivity.

Files matching any of these are shown with a preview (values redacted) and you choose **[I]nclude / [E]xclude / [S]kip** before they enter the repo.

### NEVER_INCLUDE blocklist

As a final backstop, `.ssh/id_rsa`, `.ssh/id_ed25519`, `.ssh/id_ecdsa`, and `.gnupg/` are hardcoded to `include=False` regardless of any other setting.

---

## AI Triage

AI triage is optional. Enable it by setting an LLM endpoint during init or via `config --set`:

```bash
uv run dotsync init --llm-endpoint http://localhost:4000
# or
uv run dotsync config --set llm_endpoint=http://localhost:4000
```

If the endpoint requires an API key, set `llm_api_key`. The `{env:VAR}` syntax reads from an environment variable at runtime so the key never appears in the config file:

```bash
# Store key in env (e.g. in ~/.zshrc or ~/.bashrc)
export OPENROUTER_API_KEY=sk-or-...

# Tell DotSync to read it
uv run dotsync config --set llm_api_key={env:OPENROUTER_API_KEY}
```

You can also set the key directly (less recommended):

```bash
uv run dotsync config --set llm_api_key=sk-or-v1-abc123
```

### Compatible backends

Any OpenAI-compatible endpoint works:

| Backend | Example endpoint | Needs API key? |
|---------|-----------------|----------------|
| LiteLLM proxy | `http://localhost:4000` | Depends on setup |
| Ollama | `http://localhost:11434/v1` | No |
| OpenRouter | `https://openrouter.ai/api/v1` | Yes |

### Pre-flight connectivity check

Before sending any files to the LLM, DotSync probes the endpoint with a minimal request. If it fails, you see the reason and are asked whether to continue without AI or abort:

```
Warning: LLM endpoint unreachable — auth error (check llm_api_key)
Continue without AI triage? [y/N]
```

Possible reasons: `auth error`, `model not found`, `connection refused`, `timeout`. This avoids waiting through all file batches to discover a bad endpoint.

### How classification works

The LLM is given each file's path, size, modification age, and first lines of content. It classifies using an **environment vs. infrastructure** framing:

- **Include** — file reflects a user choice (plugins, themes, keybindings, aliases) or would change behavior if lost
- **Exclude** — tool regenerates identical content on reinstall; generated/machine-written; server-pushed feature flags; addon-shipped defaults; IDE internal storage; build scaffolding
- **Ask user** — ambiguous; presented interactively

Results are cached in `~/.dotsync/classification_cache.json` so repeated `discover` runs are fast. Files are sent in batches of 10. A failed batch does not block remaining batches — those files are marked `ai:unreachable` and fall back to heuristic results.

Skip AI for a single run:

```bash
uv run dotsync discover --no-ai
```

---

## Health Checks

After every `sync` and `restore`, DotSync runs health checks to verify the system is still working. If any check fails, it automatically rolls back to the pre-operation snapshot.

### Built-in checks

Two default checks always run:

- `git --version` — verifies git is accessible
- `$SHELL -c 'echo ok'` — verifies the default shell works

### Custom checks

Add your own checks to `config.toml`:

```toml
[[health_checks]]
name = "zsh sources ok"
command = "zsh -c 'source ~/.zshrc && echo ok'"
timeout = 30

[[health_checks]]
name = "nvim config loads"
command = "nvim --headless +qa"
timeout = 15
```

Each check runs the command with `shell=False` (arguments split by `shlex`). Environment variables in the command are expanded via `os.path.expandvars`.

### Check fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | required | Human-readable label |
| `command` | string | required | Shell command to run |
| `timeout` | int | `30` | Seconds before the check is killed (exit code -1) |
| `expected_exit_code` | int | `0` | Exit code considered a pass |
| `enabled` | bool | `true` | Set to `false` to skip without removing |

### Auto-rollback behavior

When a check fails, DotSync:

1. Logs which check failed and its output
2. Calls `rollback_latest()` to restore the pre-operation snapshot
3. Raises `HealthCheckFailedError` and exits with code `1`

---

## Snapshots and Rollback

DotSync takes a local snapshot of all managed files **before every sync and restore**. Snapshots are stored in `~/.dotsync/snapshots/` and never committed to the repository.

### Listing snapshots

```bash
uv run dotsync rollback --list
```

Output shows snapshot ID, timestamp, trigger (sync/restore), file count, and hostname.

### Rolling back

```bash
# Interactive selection
uv run dotsync rollback

# Preview without writing
uv run dotsync rollback --dry-run

# Restore directly from a past sync/restore
uv run dotsync restore --from-snapshot <SNAPSHOT_ID>
```

### Retention

By default, the 5 most recent snapshots are kept; older ones are deleted automatically. Change the limit:

```bash
uv run dotsync config --set snapshot_keep=10
# Set to 0 to keep all snapshots indefinitely
uv run dotsync config --set snapshot_keep=0
```

---

## Cross-Platform Sync

When syncing between Linux and Windows (or WSL), DotSync automatically rewrites home directory paths in file contents for files with `os_profile = "shared"`.

For example, a config value `/home/alice/.config/tool` on Linux becomes `C:\Users\alice\.config\tool` on Windows when restored there, and vice versa.

Path transformation applies only to **value positions** in files (after `=`, `:`, or inside quotes) to avoid rewriting URLs or other strings that happen to contain path fragments.

Files are tagged `linux`, `windows`, or `shared` during discovery based on where they were found (`$HOME`, XDG dirs → `linux`; `%APPDATA%`, `%LOCALAPPDATA%` → `windows`; files matching both platforms → `shared`). Only files matching the current OS (or `shared`) are synced/restored.

---

## Troubleshooting

### Files not discovered

- Check `uv run dotsync discover --verbose` for rejection reasons
- Verify the file extension is in the allowed list (`.toml`, `.yaml`, `.json`, `.ini`, `.cfg`, `.conf`, `.env`, `.rc`, `.plist`, `.xml`, `.properties`, `.jsonc`, `.config`)
- Extensionless files are accepted at `$HOME` root and for `config`/`credentials` names inside subdirectories
- Add the file explicitly: `uv run dotsync config --set include_extra=["/full/path/to/file"]`

### AI triage not running

- Verify `llm_endpoint` is set: `uv run dotsync config --show`
- If the pre-flight check fails, DotSync prints the reason (`auth error`, `model not found`, `connection refused`, `timeout`) and prompts to continue without AI — check the reason before answering
- For auth errors, set `llm_api_key`: `uv run dotsync config --set llm_api_key={env:YOUR_KEY_VAR}`
- Check the endpoint is reachable: `curl <endpoint>/v1/models`
- Run with `--verbose` to see batch errors in the console and `~/.dotsync/dotsync.log`

### Health check fails after sync

DotSync will have automatically rolled back. Check the verbose log:

```bash
cat ~/.dotsync/dotsync.log | grep -A5 "health check"
```

Fix the check command or the restored file, then run `sync` again.

### Merge conflicts after pull

```
MergeConflictError: unmerged blobs in repository
```

Resolve manually:

```bash
cd ~/dotsync-repo
git status       # see conflicting files
git mergetool    # resolve conflicts
git commit
uv run dotsync restore --no-pull
```

### Verbose logging

All debug output is always written to `~/.dotsync/dotsync.log`. To also see it in the terminal:

```bash
uv run dotsync --verbose sync
```
