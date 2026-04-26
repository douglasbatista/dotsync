# Module 08 — CLI Interface

## Overview

Integration layer wiring all DotSync modules into a complete CLI using Typer and Rich. Provides 7 commands (`init`, `discover`, `sync`, `restore`, `rollback`, `status`, `config`), Rich UI helpers, sensitive file confirmation, and structured exit codes.

## Dependencies

- `config.load_config()`, `save_config()`, `default_config()`, `CONFIG_FILE`, `CONFIG_DIR`
- `discovery.discover(cfg)` → `list[ConfigFile]`
- `flagging.flag_all(files, cfg)` → `list[FlagResult]`, `enforce_never_include(files)`
- `git_ops.check_dependencies()`, `init_repo()`, `set_remote()`, `load_manifest()`, `commit_and_push()`, `pull()`
- `sync.plan_sync()`, `execute_sync()`, `plan_restore()`, `execute_restore()`, `register_new_files()`
- `snapshot.create_snapshot()`, `list_snapshots()`, `rollback()`, `verify_snapshot()`
- `health.post_operation_checks()`, `HealthCheckFailedError`
- `platform_utils.current_os()`, `home_dir()`
- `logging_setup.setup_logging()`

## Exit Codes

| Code | Name | Trigger |
|---|---|---|
| 0 | success | Normal completion |
| 1 | health_check_failed | Post-op health check failure (auto-rollback performed) |
| 2 | dependency_missing | `git` not on PATH |
| 3 | config_not_found | No `config.toml` — run `dotsync init` |
| 4 | merge_conflict | Git pull resulted in merge conflicts |
| 5 | user_aborted | User cancelled an interactive prompt |

## UI Helpers (`ui.py`)

| Function | Purpose |
|---|---|
| `print_success(msg)` | Green checkmark message |
| `print_warning(msg)` | Yellow warning message |
| `print_error(msg)` | Red error to stderr |
| `print_section(title)` | Bold rule heading |
| `file_table(files)` | Rich table of ConfigFile objects |
| `snapshot_table(snapshots)` | Rich table of SnapshotMeta objects |
| `flag_panel(flag_result)` | Rich panel for flagged sensitive file |
| `ScanStats` | Dataclass with `start_time` for tracking scan progress |
| `make_scan_display()` | Returns `Group` with animated spinner + stats table |

## Commands

### `init`

Options: `--repo-path`, `--remote`, `--llm-endpoint`

Pipeline: check_dependencies → confirm overwrite → default_config → init_repo → set_remote → save_config

### `discover`

Options: `--no-ai`, `--verbose`

Pipeline: load_config → discover(cfg) → display file_table grouped by verdict → interactive prompt for pending files

`--verbose` surfaces AI batch details (paths, timing, verdicts, errors), dir pruning, and file rejection events at DEBUG level on console. All debug output also written to `~/.dotsync/dotsync.log` regardless of `--verbose`.

### `sync`

Options: `--dry-run`, `--no-push`, `--message`

Pipeline: load_config → discover → enforce_never_include → flag_all → confirm_sensitive_files → snapshot → register_new_files → plan_sync → execute_sync → commit/push → health checks

### `restore`

Options: `--dry-run`, `--no-pull`, `--from-snapshot`

If `--from-snapshot`: bypass Git, call `rollback()` directly.
Otherwise: pull → load_manifest → snapshot → plan_restore → execute_restore → health checks

### `rollback`

Argument: `snapshot_id` (optional). Options: `--dry-run`, `--list`

`--list`: display snapshot_table and exit. No ID: interactive selection by number. Verifies snapshot integrity before rollback.

### `status`

Displays config summary, managed files count, snapshot count, git remote status.

### `config`

Options: `--show`, `--set KEY=VALUE`

`--show`: pretty-print config table. `--set`: validates key name against `DotSyncConfig.model_fields`, coerces value type, saves.

## Sensitive File Confirmation

`confirm_sensitive_files(flag_results)` in `main.py` — thin UI function:

- Prompts for each `requires_confirmation=True` file
- `I` (Include): clear `requires_confirmation`
- `E` (Exclude): set `include=False`, clear `requires_confirmation`
- `S` (Skip): leave as-is

## Testing

11 tests in `tests/test_cli.py`:

- **TestConfirmSensitiveFiles** (3): Include/Exclude/Skip flows
- **TestMarkSensitive**: post-confirmation helper sets `ConfigFile.sensitive=True` for files with detections
- **TestConfigCommand** (2): set valid key, reject unknown key
- **TestErrorHandling** (2): health check failure exit code, missing dependency exit code
- Additional CLI tests covering confirmation, config, and error handling

## Design Decisions

1. Late imports inside commands — avoids circular imports, follows existing pattern
2. `confirm_sensitive_files()` lives in `main.py` — UI concern, not business logic
3. `typer.Exit(code=N)` for structured exit codes
4. Rich status spinners for long operations
5. `config --set` validates key names against Pydantic model fields
6. All debug output unified through Python `logging` → `~/.dotsync/dotsync.log` (always) + console (with `--verbose`). No separate debug files.
