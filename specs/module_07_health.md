# Module 07 — Health Checks

## Overview

Post-operation health checks — configurable shell commands that run after sync/restore to verify the system is still healthy. If any check fails, triggers an automatic rollback to the pre-operation snapshot and raises a clear error.

## Dependencies

- `config.DotSyncConfig.health_checks` → `list[str]`, default `[]` — user-configured check commands
- `snapshot.rollback(snapshot_id, home)` → restores files from a snapshot
- `snapshot.SnapshotNotFoundError` → raised if snapshot missing during rollback
- `logging.getLogger("dotsync")` — project-wide logging pattern

## Data Model

### `HealthCheck` (dataclass)

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | `str` | — | Human-readable check name |
| `command` | `str` | — | Shell command to execute |
| `timeout_seconds` | `int` | `10` | Max seconds before timeout |
| `expected_exit_code` | `int` | `0` | Exit code that means success |
| `enabled` | `bool` | `True` | Whether to run this check |

### `HealthCheckResult` (dataclass)

| Field | Type | Description |
|---|---|---|
| `check` | `HealthCheck` | The check that was run |
| `passed` | `bool` | Whether exit code matched expected |
| `exit_code` | `int` | Actual exit code (`-1` = timeout, `-2` = not found) |
| `stdout` | `str` | Captured standard output |
| `stderr` | `str` | Captured standard error |
| `duration_ms` | `int` | Execution time in milliseconds |

### `HealthCheckFailedError` (Exception)

Raised after automatic rollback when one or more health checks fail.

## Constants

### `DEFAULT_CHECKS`

```python
DEFAULT_CHECKS: list[HealthCheck] = [
    HealthCheck(name="git", command="git --version"),
    HealthCheck(name="shell", command="${SHELL} -c 'echo ok'"),
]
```

## Steps

### Step 7.1 — Data model & defaults

- Define `HealthCheck`, `HealthCheckResult`, and `HealthCheckFailedError`
- Define `DEFAULT_CHECKS` list

### Step 7.2 — Single check runner

`run_check(check: HealthCheck) -> HealthCheckResult`

- Expand env vars with `os.path.expandvars`
- Split command with `shlex.split` (shell=False for safety)
- Execute with `subprocess.run(capture_output=True, text=True, timeout=...)`
- Handle `TimeoutExpired` → `exit_code=-1, stderr="timeout"`
- Handle `FileNotFoundError` → `exit_code=-2, stderr="command not found: <cmd>"`
- Measure duration via `time.monotonic_ns()` → convert to ms

### Step 7.3 — Batch runner

`run_all_checks(cfg, extra_checks=None) -> list[HealthCheckResult]`

- Build check list: `DEFAULT_CHECKS` + user checks from `cfg.health_checks` + `extra_checks`
- Convert user command strings to `HealthCheck` objects (name = first token)
- Filter to `enabled=True` only
- Execute sequentially (cascading failure detection)

`all_passed(results) -> bool` — returns `all(r.passed for r in results)`

### Step 7.4 — Auto-rollback trigger

`check_and_rollback_if_needed(results, snapshot_id, home) -> bool`

- If `all_passed(results)`: return `False`
- Otherwise: call `snapshot.rollback(snapshot_id, home)`
- Log failed checks (name, command, exit_code, stderr snippet)
- Log rollback performed
- Raise `HealthCheckFailedError` with summary of failed checks

### Step 7.5 — Post-operation orchestration

`post_operation_checks(cfg, snapshot_id, home, operation) -> None`

- Run `run_all_checks(cfg)`
- If no results (empty list): log warning, return
- Call `check_and_rollback_if_needed(results, snapshot_id, home)`
- Log success if all pass

## Acceptance Criteria

- 19 tests pass covering data model, runner, batch, rollback, and orchestration
- No external commands executed in tests (all subprocess calls mocked)
- `ruff check` and `mypy` pass
- `HealthCheckFailedError` includes names of failed checks and snapshot ID
- Default checks verify git and shell availability
- Auto-rollback calls `snapshot.rollback` before raising
