"""Post-operation health checks for DotSync.

Runs configurable shell commands after sync/restore to verify the system
is still healthy.  If any check fails, triggers an automatic rollback to
the pre-operation snapshot and raises a clear error.
"""

from __future__ import annotations

import logging
import os
import platform
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotsync import snapshot
from dotsync.config import DotSyncConfig

logger = logging.getLogger("dotsync")

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class HealthCheck:
    """A single health check definition."""

    name: str
    command: str
    timeout_seconds: int = 10
    expected_exit_code: int = 0
    enabled: bool = True


@dataclass
class HealthCheckResult:
    """Result of running a single health check."""

    check: HealthCheck
    passed: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


class HealthCheckFailedError(Exception):
    """Raised when one or more health checks fail after rollback."""


# ---------------------------------------------------------------------------
# Default checks
# ---------------------------------------------------------------------------

def _default_shell_check() -> str:
    """Return a platform-appropriate shell health check command."""
    if platform.system() == "Windows":
        return "cmd /c echo ok"
    return "${SHELL} -c 'echo ok'"


DEFAULT_CHECKS: list[HealthCheck] = [
    HealthCheck(name="git", command="git --version"),
    HealthCheck(name="shell", command=_default_shell_check()),
]

# ---------------------------------------------------------------------------
# Single check runner
# ---------------------------------------------------------------------------


def run_check(check: HealthCheck) -> HealthCheckResult:
    """Execute a single health check command.

    Uses ``subprocess.run`` with ``shell=False`` after expanding environment
    variables and splitting with ``shlex.split``.

    Args:
        check: The health check to execute.

    Returns:
        A result indicating pass/fail, exit code, output, and duration.
    """
    expanded = os.path.expandvars(check.command)
    try:
        argv = shlex.split(expanded)
    except ValueError:
        return HealthCheckResult(
            check=check,
            passed=False,
            exit_code=-3,
            stdout="",
            stderr=f"invalid command syntax: {check.command}",
            duration_ms=0,
        )

    start = time.monotonic_ns()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=check.timeout_seconds,
        )
        duration_ms = (time.monotonic_ns() - start) // 1_000_000
        passed = proc.returncode == check.expected_exit_code
        return HealthCheckResult(
            check=check,
            passed=passed,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration_ms=duration_ms,
        )
    except subprocess.TimeoutExpired:
        duration_ms = (time.monotonic_ns() - start) // 1_000_000
        return HealthCheckResult(
            check=check,
            passed=False,
            exit_code=-1,
            stdout="",
            stderr="timeout",
            duration_ms=duration_ms,
        )
    except FileNotFoundError:
        duration_ms = (time.monotonic_ns() - start) // 1_000_000
        cmd_name = argv[0] if argv else check.command
        return HealthCheckResult(
            check=check,
            passed=False,
            exit_code=-2,
            stdout="",
            stderr=f"command not found: {cmd_name}",
            duration_ms=duration_ms,
        )


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------


def _user_checks_from_config(cfg: DotSyncConfig) -> list[HealthCheck]:
    """Convert user command strings to HealthCheck objects."""
    checks: list[HealthCheck] = []
    for cmd in cfg.health_checks:
        name = cmd.split()[0] if cmd.strip() else cmd
        checks.append(HealthCheck(name=name, command=cmd))
    return checks


def run_all_checks(
    cfg: DotSyncConfig,
    extra_checks: list[HealthCheck] | None = None,
) -> list[HealthCheckResult]:
    """Run all health checks sequentially.

    Combines default checks, user-configured checks from ``cfg.health_checks``,
    and any ``extra_checks``.  Only enabled checks are executed.

    Args:
        cfg: DotSync configuration (provides user check commands).
        extra_checks: Additional checks to append.

    Returns:
        List of results in execution order.
    """
    all_checks = list(DEFAULT_CHECKS) + _user_checks_from_config(cfg) + (extra_checks or [])
    enabled = [c for c in all_checks if c.enabled]

    results: list[HealthCheckResult] = []
    for check in enabled:
        result = run_check(check)
        results.append(result)
        logger.debug(
            "health check %r: %s (exit=%d, %dms)",
            check.name,
            "PASS" if result.passed else "FAIL",
            result.exit_code,
            result.duration_ms,
        )
    return results


def all_passed(results: list[HealthCheckResult]) -> bool:
    """Return True if every result passed."""
    return all(r.passed for r in results)


# ---------------------------------------------------------------------------
# Auto-rollback trigger
# ---------------------------------------------------------------------------


def check_and_rollback_if_needed(
    results: list[HealthCheckResult],
    snapshot_id: str,
    home: Path,
) -> bool:
    """Trigger automatic rollback if any health check failed.

    Args:
        results: Health check results to evaluate.
        snapshot_id: Pre-operation snapshot to rollback to.
        home: Home directory root for rollback.

    Returns:
        False if all checks passed (no rollback needed).

    Raises:
        HealthCheckFailedError: After performing rollback, with a summary
            of which checks failed.
    """
    if all_passed(results):
        return False

    failed = [r for r in results if not r.passed]
    for r in failed:
        stderr_snippet = r.stderr[:200] if r.stderr else "(no output)"
        logger.error(
            "health check FAILED: %s (command=%r, exit=%d, stderr=%s)",
            r.check.name,
            r.check.command,
            r.exit_code,
            stderr_snippet,
        )

    snapshot.rollback(snapshot_id, home)
    logger.warning("automatic rollback performed to snapshot %s", snapshot_id)

    names = ", ".join(r.check.name for r in failed)
    raise HealthCheckFailedError(
        f"Health checks failed: {names}. "
        f"Automatic rollback to snapshot {snapshot_id} completed."
    )


# ---------------------------------------------------------------------------
# Post-operation orchestration
# ---------------------------------------------------------------------------


def post_operation_checks(
    cfg: DotSyncConfig,
    snapshot_id: str,
    home: Path,
    operation: Literal["sync", "restore"],
) -> None:
    """Run health checks after a sync or restore operation.

    This is the single integration point for the CLI layer.
    If any check fails, an automatic rollback is triggered.

    Args:
        cfg: DotSync configuration.
        snapshot_id: Pre-operation snapshot ID for rollback.
        home: Home directory root.
        operation: Which operation just completed.
    """
    results = run_all_checks(cfg)
    if not results:
        logger.warning("no health checks configured or available for %s", operation)
        return

    check_and_rollback_if_needed(results, snapshot_id, home)
    logger.info("all health checks passed after %s", operation)
