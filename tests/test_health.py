"""Tests for dotsync.health — post-operation health checks."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dotsync.config import DotSyncConfig
from dotsync.health import (
    DEFAULT_CHECKS,
    HealthCheck,
    HealthCheckFailedError,
    HealthCheckResult,
    all_passed,
    check_and_rollback_if_needed,
    post_operation_checks,
    run_all_checks,
    run_check,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cfg(**overrides) -> DotSyncConfig:
    """Create a DotSyncConfig with sensible defaults."""
    defaults = {"repo_path": Path("/tmp/test-repo")}
    defaults.update(overrides)
    return DotSyncConfig(**defaults)


def _pass_result(check: HealthCheck | None = None) -> HealthCheckResult:
    """Create a passing HealthCheckResult."""
    c = check or HealthCheck(name="ok", command="true")
    return HealthCheckResult(check=c, passed=True, exit_code=0, stdout="", stderr="", duration_ms=1)


def _fail_result(check: HealthCheck | None = None) -> HealthCheckResult:
    """Create a failing HealthCheckResult."""
    c = check or HealthCheck(name="bad", command="false")
    return HealthCheckResult(check=c, passed=False, exit_code=1, stdout="", stderr="error", duration_ms=1)


# ===================================================================
# TestDataModel (Step 7.1) — 2 tests
# ===================================================================


class TestDataModel:
    """Tests for HealthCheck and HealthCheckResult data models."""

    def test_default_checks_are_valid_health_check_objects(self) -> None:
        """DEFAULT_CHECKS contains valid HealthCheck instances."""
        assert len(DEFAULT_CHECKS) >= 2
        for check in DEFAULT_CHECKS:
            assert isinstance(check, HealthCheck)
            assert check.name
            assert check.command
            assert check.timeout_seconds > 0
            assert check.enabled is True

    def test_health_check_result_has_required_fields(self) -> None:
        """HealthCheckResult exposes all required attributes."""
        check = HealthCheck(name="test", command="echo hi")
        result = HealthCheckResult(
            check=check,
            passed=True,
            exit_code=0,
            stdout="hi\n",
            stderr="",
            duration_ms=42,
        )
        assert result.check is check
        assert result.passed is True
        assert result.exit_code == 0
        assert result.stdout == "hi\n"
        assert result.stderr == ""
        assert result.duration_ms == 42


# ===================================================================
# TestRunCheck (Step 7.2) — 5 tests
# ===================================================================


class TestRunCheck:
    """Tests for the single-check runner."""

    @patch("dotsync.health.subprocess.run")
    def test_run_check_passes_on_success(self, mock_run: MagicMock) -> None:
        """A check passes when subprocess returns the expected exit code."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "--version"], returncode=0, stdout="git 2.40\n", stderr=""
        )
        check = HealthCheck(name="git", command="git --version")
        result = run_check(check)

        assert result.passed is True
        assert result.exit_code == 0
        assert result.stdout == "git 2.40\n"

    @patch("dotsync.health.subprocess.run")
    def test_run_check_fails_on_nonzero_exit(self, mock_run: MagicMock) -> None:
        """A check fails when subprocess returns a non-zero exit code."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["false"], returncode=1, stdout="", stderr="fail"
        )
        check = HealthCheck(name="fail", command="false")
        result = run_check(check)

        assert result.passed is False
        assert result.exit_code == 1

    @patch("dotsync.health.subprocess.run")
    def test_run_check_handles_timeout(self, mock_run: MagicMock) -> None:
        """TimeoutExpired yields passed=False with exit_code=-1."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="sleep", timeout=10)
        check = HealthCheck(name="slow", command="sleep 999")
        result = run_check(check)

        assert result.passed is False
        assert result.exit_code == -1
        assert "timeout" in result.stderr

    @patch("dotsync.health.subprocess.run")
    def test_run_check_handles_command_not_found(self, mock_run: MagicMock) -> None:
        """FileNotFoundError yields passed=False with exit_code=-2."""
        mock_run.side_effect = FileNotFoundError()
        check = HealthCheck(name="missing", command="nonexistent_cmd --flag")
        result = run_check(check)

        assert result.passed is False
        assert result.exit_code == -2
        assert "command not found" in result.stderr
        assert "nonexistent_cmd" in result.stderr

    @patch("dotsync.health.subprocess.run")
    def test_run_check_measures_duration(self, mock_run: MagicMock) -> None:
        """duration_ms is a non-negative integer."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["echo"], returncode=0, stdout="", stderr=""
        )
        check = HealthCheck(name="fast", command="echo hi")
        result = run_check(check)

        assert isinstance(result.duration_ms, int)
        assert result.duration_ms >= 0


# ===================================================================
# TestBatchRunner (Step 7.3) — 4 tests
# ===================================================================


class TestBatchRunner:
    """Tests for the batch check runner."""

    @patch("dotsync.health.run_check")
    def test_run_all_includes_defaults_and_user_checks(self, mock_rc: MagicMock) -> None:
        """run_all_checks combines DEFAULT_CHECKS with user commands."""
        mock_rc.return_value = _pass_result()
        cfg = _make_cfg(health_checks=["mypy --version"])
        results = run_all_checks(cfg)

        # DEFAULT_CHECKS (2) + user (1) = 3
        assert len(results) == len(DEFAULT_CHECKS) + 1
        # Verify user check was included
        check_names = [call.args[0].name for call in mock_rc.call_args_list]
        assert "mypy" in check_names

    def test_all_passed_true_when_all_pass(self) -> None:
        """all_passed returns True when every result passed."""
        results = [_pass_result(), _pass_result()]
        assert all_passed(results) is True

    def test_all_passed_false_when_any_fails(self) -> None:
        """all_passed returns False when at least one result failed."""
        results = [_pass_result(), _fail_result()]
        assert all_passed(results) is False

    @patch("dotsync.health.run_check")
    def test_run_all_includes_extra_checks(self, mock_rc: MagicMock) -> None:
        """extra_checks are appended after defaults and user checks."""
        mock_rc.return_value = _pass_result()
        cfg = _make_cfg()
        extra = [HealthCheck(name="extra", command="extra --test")]
        results = run_all_checks(cfg, extra_checks=extra)

        assert len(results) == len(DEFAULT_CHECKS) + 1
        check_names = [call.args[0].name for call in mock_rc.call_args_list]
        assert "extra" in check_names


# ===================================================================
# TestAutoRollback (Step 7.4) — 4 tests
# ===================================================================


class TestAutoRollback:
    """Tests for the automatic rollback trigger."""

    def test_no_rollback_when_all_pass(self, tmp_path: Path) -> None:
        """Returns False with no rollback when all checks pass."""
        results = [_pass_result(), _pass_result()]
        assert check_and_rollback_if_needed(results, "snap-1", tmp_path) is False

    @patch("dotsync.health.snapshot.rollback")
    def test_rollback_triggered_when_check_fails(
        self, mock_rollback: MagicMock, tmp_path: Path
    ) -> None:
        """snapshot.rollback is called when any check fails."""
        results = [_pass_result(), _fail_result()]
        with pytest.raises(HealthCheckFailedError):
            check_and_rollback_if_needed(results, "snap-1", tmp_path)
        mock_rollback.assert_called_once_with("snap-1", tmp_path)

    @patch("dotsync.health.snapshot.rollback")
    def test_rollback_raises_health_check_failed_error(
        self, mock_rollback: MagicMock, tmp_path: Path
    ) -> None:
        """HealthCheckFailedError is raised after rollback."""
        results = [_fail_result()]
        with pytest.raises(HealthCheckFailedError, match="Health checks failed"):
            check_and_rollback_if_needed(results, "snap-1", tmp_path)

    @patch("dotsync.health.snapshot.rollback")
    def test_failed_check_details_included_in_error(
        self, mock_rollback: MagicMock, tmp_path: Path
    ) -> None:
        """Error message includes names of failed checks and snapshot ID."""
        fail1 = _fail_result(HealthCheck(name="check_a", command="a"))
        fail2 = _fail_result(HealthCheck(name="check_b", command="b"))
        results = [_pass_result(), fail1, fail2]

        with pytest.raises(HealthCheckFailedError, match="check_a") as exc_info:
            check_and_rollback_if_needed(results, "snap-42", tmp_path)

        msg = str(exc_info.value)
        assert "check_a" in msg
        assert "check_b" in msg
        assert "snap-42" in msg


# ===================================================================
# TestPostOperation (Step 7.5) — 4 tests
# ===================================================================


class TestPostOperation:
    """Tests for the post-operation orchestration function."""

    @patch("dotsync.health.run_all_checks", return_value=[])
    def test_post_operation_no_op_when_no_checks_configured(
        self, mock_rac: MagicMock, tmp_path: Path
    ) -> None:
        """Logs warning and returns when no checks are available."""
        cfg = _make_cfg(health_checks=[])
        # Should not raise
        post_operation_checks(cfg, "snap-1", tmp_path, "sync")

    @patch("dotsync.health.snapshot.rollback")
    @patch("dotsync.health.run_all_checks")
    def test_post_operation_raises_on_failure(
        self, mock_rac: MagicMock, mock_rollback: MagicMock, tmp_path: Path
    ) -> None:
        """HealthCheckFailedError propagates from post_operation_checks."""
        mock_rac.return_value = [_fail_result()]
        cfg = _make_cfg()
        with pytest.raises(HealthCheckFailedError):
            post_operation_checks(cfg, "snap-1", tmp_path, "sync")

    @patch("dotsync.health.snapshot.rollback")
    @patch("dotsync.health.run_all_checks")
    def test_post_operation_passes_snapshot_id_to_rollback(
        self, mock_rac: MagicMock, mock_rollback: MagicMock, tmp_path: Path
    ) -> None:
        """The correct snapshot_id is forwarded to snapshot.rollback."""
        mock_rac.return_value = [_fail_result()]
        cfg = _make_cfg()
        with pytest.raises(HealthCheckFailedError):
            post_operation_checks(cfg, "snap-xyz", tmp_path, "restore")
        mock_rollback.assert_called_once_with("snap-xyz", tmp_path)

    @patch("dotsync.health.run_all_checks")
    def test_post_operation_succeeds_when_all_pass(
        self, mock_rac: MagicMock, tmp_path: Path
    ) -> None:
        """No exception when all health checks pass."""
        mock_rac.return_value = [_pass_result(), _pass_result()]
        cfg = _make_cfg()
        # Should not raise
        post_operation_checks(cfg, "snap-1", tmp_path, "sync")
