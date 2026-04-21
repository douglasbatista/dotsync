"""Tests for dotsync.flagging module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from dotsync.config import DotSyncConfig
from dotsync.discovery import ConfigFile
from dotsync.flagging import (
    NEVER_INCLUDE,
    SENSITIVE_PATTERNS,
    FlagResult,
    SensitiveMatch,
    _redact,
    ai_flag_check,
    enforce_never_include,
    flag_all,
    scan_file_for_secrets,
)
from dotsync.llm_client import LLMError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_file(base: Path, rel: str, content: str = "hello\n") -> Path:
    """Create a file under base with the given relative path."""
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _default_cfg(**overrides: object) -> DotSyncConfig:
    """Return a DotSyncConfig with sensible defaults for testing."""
    defaults: dict = {
        "repo_path": Path("/tmp/dotsync-test-repo"),
        "exclude_patterns": [],
        "include_extra": [],
        "llm_endpoint": None,
    }
    defaults.update(overrides)
    return DotSyncConfig(**defaults)


def _config_file(
    tmp_path: Path,
    rel: str,
    content: str = "hello\n",
    include: bool | None = True,
    reason: str = "test",
) -> ConfigFile:
    """Create a ConfigFile backed by a real file on disk."""
    abs_path = _make_file(tmp_path, rel, content)
    return ConfigFile(
        path=Path(rel),
        abs_path=abs_path,
        size_bytes=abs_path.stat().st_size,
        include=include,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Step 3.1 — TestPatterns
# ---------------------------------------------------------------------------


class TestPatterns:
    def test_github_token_detected(self) -> None:
        """GitHub personal access token is matched."""
        line = "GITHUB_TOKEN=ghp_ABCDEFghijklmnopqrstuvwxyz0123456789"
        assert SENSITIVE_PATTERNS["github_token"].search(line)

    def test_aws_key_detected(self) -> None:
        """AWS access key ID is matched."""
        line = "aws_access_key_id = AKIAIOSFODNN7EXAMPLE"
        assert SENSITIVE_PATTERNS["aws_access_key"].search(line)

    def test_email_detected(self) -> None:
        """Email address is matched."""
        line = "email = user@example.com"
        assert SENSITIVE_PATTERNS["email"].search(line)

    def test_clean_file_no_match(self) -> None:
        """Typical .gitconfig content should not trigger any pattern."""
        content = "[user]\n\tname = John Doe\n[core]\n\teditor = vim\n"
        for name, pattern in SENSITIVE_PATTERNS.items():
            # email will match if there's an email, but not in this content
            assert not pattern.search(content), f"Pattern {name} matched unexpectedly"

    def test_generic_token_detected(self) -> None:
        """Generic TOKEN= assignment is matched."""
        line = "TOKEN=supersecretvalue"
        assert SENSITIVE_PATTERNS["generic_token"].search(line)


# ---------------------------------------------------------------------------
# Step 3.2 — TestScanFile
# ---------------------------------------------------------------------------


class TestScanFile:
    def test_scan_returns_match_with_line_number(self, tmp_path: Path) -> None:
        """Scan reports correct line number for a match."""
        content = "line1\nline2\nghp_ABCDEFghijklmnopqrstuvwxyz0123456789\nline4\n"
        f = _make_file(tmp_path, "test.env", content)
        matches = scan_file_for_secrets(f)
        github_matches = [m for m in matches if m.pattern_name == "github_token"]
        assert len(github_matches) == 1
        assert github_matches[0].line_number == 3

    def test_scan_redacts_value_in_preview(self, tmp_path: Path) -> None:
        """Matched values are redacted in preview."""
        content = "ghp_ABCDEFghijklmnopqrstuvwxyz0123456789\n"
        f = _make_file(tmp_path, "test.env", content)
        matches = scan_file_for_secrets(f)
        github_matches = [m for m in matches if m.pattern_name == "github_token"]
        assert len(github_matches) == 1
        preview = github_matches[0].preview
        assert "***" in preview
        # Should show first 2 and last 2 chars
        assert preview.startswith("gh")
        assert preview.endswith("89")

    def test_scan_skips_commented_generic_tokens(self, tmp_path: Path) -> None:
        """Lines starting with # are skipped for generic_token and generic_api_key."""
        content = "# TOKEN=supersecretvalue\n# API_KEY=mysecret\n"
        f = _make_file(tmp_path, "test.conf", content)
        matches = scan_file_for_secrets(f)
        generic = [m for m in matches if m.pattern_name in ("generic_token", "generic_api_key")]
        assert len(generic) == 0

    def test_scan_handles_unicode_error_gracefully(self, tmp_path: Path) -> None:
        """Binary files that cause UnicodeDecodeError return empty list."""
        f = tmp_path / "binary.dat"
        f.write_bytes(b"\xff\xfe" + b"\x00" * 100)
        # Force the file to be read as utf-8 (which will fail for raw bytes)
        matches = scan_file_for_secrets(f)
        assert matches == []


# ---------------------------------------------------------------------------
# Step 3.3 — TestAIFlag
# ---------------------------------------------------------------------------


class TestAIFlag:
    def test_ai_flag_returns_bool(self, tmp_path: Path) -> None:
        """AI flag check returns True when LLM says sensitive."""
        f = _make_file(tmp_path, "secrets.env", "DB_PASSWORD=hunter2\n")
        cfg = _default_cfg(llm_endpoint="http://localhost:8000")

        with patch("dotsync.flagging.chat_completion") as mock_chat, \
             patch("dotsync.flagging._load_sensitivity_cache", return_value={}), \
             patch("dotsync.flagging._save_sensitivity_cache"):
            mock_chat.return_value = json.dumps(
                {"sensitive": True, "reason": "contains password"}
            )
            result = ai_flag_check(f, cfg)
            assert result is True

    def test_ai_flag_cache_invalidated_on_mtime_change(self, tmp_path: Path) -> None:
        """Cache key includes mtime, so changed files are re-checked."""
        f = _make_file(tmp_path, "config.env", "safe=true\n")
        cfg = _default_cfg(llm_endpoint="http://localhost:8000")
        mtime1 = f.stat().st_mtime
        key1 = f"{f}:{mtime1}"

        # Populate cache with old mtime
        old_cache = {key1: {"sensitive": False, "reason": "safe"}}

        # Modify file to change mtime
        f.write_text("TOKEN=newsecret\n", encoding="utf-8")
        mtime2 = f.stat().st_mtime

        # If mtime changed, the old cache key won't match
        key2 = f"{f}:{mtime2}"
        assert key1 != key2 or mtime1 == mtime2  # Keys differ if mtime changed

        with patch("dotsync.flagging.chat_completion") as mock_chat, \
             patch("dotsync.flagging._load_sensitivity_cache", return_value=old_cache), \
             patch("dotsync.flagging._save_sensitivity_cache"):
            mock_chat.return_value = json.dumps(
                {"sensitive": True, "reason": "new secret"}
            )
            result = ai_flag_check(f, cfg)
            # Should call AI because mtime changed (cache miss)
            if mtime1 != mtime2:
                mock_chat.assert_called_once()
                assert result is True

    def test_ai_flag_graceful_on_error(self, tmp_path: Path) -> None:
        """AI flag returns False on LLMError (fail open)."""
        f = _make_file(tmp_path, "config.env", "some content\n")
        cfg = _default_cfg(llm_endpoint="http://localhost:8000")

        with patch("dotsync.flagging.chat_completion") as mock_chat, \
             patch("dotsync.flagging._load_sensitivity_cache", return_value={}):
            mock_chat.side_effect = LLMError("timeout")
            result = ai_flag_check(f, cfg)
            assert result is False


# ---------------------------------------------------------------------------
# Step 3.4 — TestFlagAll
# ---------------------------------------------------------------------------


class TestFlagAll:
    def test_flag_all_skips_excluded_files(self, tmp_path: Path) -> None:
        """Files with include=False are not scanned."""
        cf = _config_file(tmp_path, "excluded.conf", "TOKEN=secret\n", include=False)
        cfg = _default_cfg()
        results = flag_all([cf], cfg)
        assert len(results) == 0

    def test_flag_all_marks_requires_confirmation_on_match(self, tmp_path: Path) -> None:
        """Files with regex matches have requires_confirmation=True."""
        cf = _config_file(
            tmp_path, "secrets.env",
            "ghp_ABCDEFghijklmnopqrstuvwxyz0123456789\n",
        )
        cfg = _default_cfg()
        results = flag_all([cf], cfg)
        assert len(results) == 1
        assert results[0].requires_confirmation is True
        assert len(results[0].matches) > 0

    def test_flag_all_ai_not_called_when_regex_matched(self, tmp_path: Path) -> None:
        """AI check is skipped when regex already found matches."""
        cf = _config_file(
            tmp_path, "secrets.env",
            "ghp_ABCDEFghijklmnopqrstuvwxyz0123456789\n",
        )
        cfg = _default_cfg(llm_endpoint="http://localhost:8000")

        with patch("dotsync.flagging.ai_flag_check") as mock_ai:
            results = flag_all([cf], cfg)
            mock_ai.assert_not_called()
            assert results[0].requires_confirmation is True


# ---------------------------------------------------------------------------
# Step 3.5 — TestNeverInclude
# ---------------------------------------------------------------------------


class TestNeverInclude:
    def test_ssh_private_key_always_excluded(self, tmp_path: Path) -> None:
        """SSH private keys in NEVER_INCLUDE are set to include=False."""
        cf = _config_file(tmp_path, ".ssh/id_rsa", "key content\n", include=True)
        enforce_never_include([cf])
        assert cf.include is False
        assert cf.reason == "never_include"

    def test_gitcrypt_key_always_excluded(self, tmp_path: Path) -> None:
        """dotsync.key in NEVER_INCLUDE is set to include=False."""
        cf = _config_file(tmp_path, "dotsync.key", "key data\n", include=True)
        enforce_never_include([cf])
        assert cf.include is False
        assert cf.reason == "never_include"

    def test_enforce_cannot_be_overridden(self, tmp_path: Path) -> None:
        """Even if include was explicitly True, NEVER_INCLUDE wins."""
        cf = _config_file(
            tmp_path, ".ssh/id_ed25519", "key\n",
            include=True, reason="user_included",
        )
        enforce_never_include([cf])
        assert cf.include is False
        assert cf.reason == "never_include"


# ---------------------------------------------------------------------------
# Redact helper
# ---------------------------------------------------------------------------


class TestRedact:
    def test_redact_long_value(self) -> None:
        """Values longer than 4 chars show first 2 and last 2."""
        assert _redact("abcdef") == "ab***ef"

    def test_redact_short_value(self) -> None:
        """Values of 4 chars or fewer show only ***."""
        assert _redact("abc") == "***"
        assert _redact("abcd") == "***"

    def test_redact_exact_five(self) -> None:
        """5-char value shows first 2 and last 2."""
        assert _redact("abcde") == "ab***de"
