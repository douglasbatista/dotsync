"""Tests for dotsync.discovery module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from dotsync.config import DotSyncConfig
from dotsync.discovery import (
    HARDCODED_EXCLUDES,
    KNOWN_FILES,
    ConfigFile,
    _load_classification_cache,
    _save_classification_cache,
    classify_rule_based,
    classify_with_ai,
    discover,
    scan_candidates,
)
from dotsync.llm_client import LLMError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_file(base: Path, rel: str, content: str = "hello\n", size: int | None = None) -> Path:
    """Create a file under base with the given relative path."""
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    if size is not None:
        # Pad or truncate to exact size
        p.write_bytes(content.encode()[:size].ljust(size, b" "))
    return p


def _default_cfg(**overrides) -> DotSyncConfig:
    """Return a DotSyncConfig with sensible defaults for testing."""
    defaults = {
        "repo_path": Path("/tmp/dotsync-test-repo"),
        "exclude_patterns": [],
        "include_extra": [],
        "llm_endpoint": None,
    }
    defaults.update(overrides)
    return DotSyncConfig(**defaults)


# ---------------------------------------------------------------------------
# Step 2.1 — Allowlist / exclude sanity
# ---------------------------------------------------------------------------


class TestAllowlists:
    def test_known_files_are_valid_relative_paths(self) -> None:
        """KNOWN_FILES entries should be relative (no leading slash)."""
        for f in KNOWN_FILES:
            assert not f.startswith("/"), f"KNOWN_FILES entry should be relative: {f}"

    def test_hardcoded_excludes_not_in_known_files(self) -> None:
        """No overlap between hardcoded excludes and known files."""
        for exc in HARDCODED_EXCLUDES:
            assert exc not in KNOWN_FILES, f"Exclude pattern also in KNOWN_FILES: {exc}"


# ---------------------------------------------------------------------------
# Step 2.2 — scan_candidates
# ---------------------------------------------------------------------------


class TestScanCandidates:
    def test_scan_excludes_ssh_private_keys(self, tmp_path: Path) -> None:
        _make_file(tmp_path, ".ssh/id_rsa", "secret")
        _make_file(tmp_path, ".ssh/config", "Host *")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[tmp_path]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "id_rsa" not in names
        assert "config" in names

    def test_scan_skips_large_files(self, tmp_path: Path) -> None:
        _make_file(tmp_path, "small.conf", "x")
        _make_file(tmp_path, "huge.conf", "x", size=2_000_000)

        with (
            patch("dotsync.discovery.config_dirs", return_value=[tmp_path]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "small.conf" in names
        assert "huge.conf" not in names

    def test_scan_skips_binary_files(self, tmp_path: Path) -> None:
        text_file = tmp_path / "text.conf"
        text_file.write_text("hello world\n")

        bin_file = tmp_path / "binary.dat"
        bin_file.write_bytes(b"hello\x00world")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[tmp_path]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "text.conf" in names
        assert "binary.dat" not in names

    def test_scan_respects_max_depth(self, tmp_path: Path) -> None:
        # Depth 0 (root) -> depth 1 -> ... -> depth 5
        _make_file(tmp_path, "a/b/c/shallow.conf", "ok")  # depth 3
        _make_file(tmp_path, "a/b/c/d/e/deep.conf", "too deep")  # depth 5

        with (
            patch("dotsync.discovery.config_dirs", return_value=[tmp_path]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "shallow.conf" in names
        assert "deep.conf" not in names

    def test_scan_includes_extra_paths(self, tmp_path: Path) -> None:
        extra_file = tmp_path / "special" / "my.conf"
        extra_file.parent.mkdir(parents=True)
        extra_file.write_text("extra")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[tmp_path]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates(extra_paths=[extra_file])

        assert extra_file in results


# ---------------------------------------------------------------------------
# Step 2.3 — classify_rule_based
# ---------------------------------------------------------------------------


class TestClassifyRuleBased:
    def test_known_file_included(self, tmp_path: Path) -> None:
        f = _make_file(tmp_path, ".bashrc", "# bash config")

        with patch("dotsync.discovery.home_dir", return_value=tmp_path):
            result = classify_rule_based([f])

        assert len(result) == 1
        assert result[0].include is True
        assert result[0].reason == "known"

    def test_user_excluded_pattern(self, tmp_path: Path) -> None:
        f = _make_file(tmp_path, ".bashrc", "# bash config")

        with patch("dotsync.discovery.home_dir", return_value=tmp_path):
            result = classify_rule_based([f], exclude_patterns=[".*bashrc*"])

        assert len(result) == 1
        assert result[0].include is False
        assert result[0].reason == "user_excluded"

    def test_unknown_file_pending(self, tmp_path: Path) -> None:
        f = _make_file(tmp_path, "random_app.conf", "stuff")

        with patch("dotsync.discovery.home_dir", return_value=tmp_path):
            result = classify_rule_based([f])

        assert len(result) == 1
        assert result[0].include is None
        assert result[0].reason == "unknown"

    def test_os_profile_windows(self, tmp_path: Path) -> None:
        f = _make_file(tmp_path, "AppData/Roaming/app.conf", "win config")

        with patch("dotsync.discovery.home_dir", return_value=tmp_path):
            result = classify_rule_based([f])

        assert len(result) == 1
        assert result[0].os_profile == "windows"


# ---------------------------------------------------------------------------
# Step 2.4 — classify_with_ai
# ---------------------------------------------------------------------------


class TestClassifyWithAI:
    def _make_candidate(self, tmp_path: Path, rel: str) -> ConfigFile:
        f = _make_file(tmp_path, rel, "content")
        return ConfigFile(
            path=Path(rel),
            abs_path=f,
            size_bytes=7,
            include=None,
            reason="unknown",
        )

    def test_ai_classify_returns_valid_verdicts(self, tmp_path: Path) -> None:
        cfg = _default_cfg(llm_endpoint="http://localhost:8000")
        cf = self._make_candidate(tmp_path, "myapp.conf")

        ai_response = json.dumps([{"path": "myapp.conf", "verdict": "include"}])

        with (
            patch("dotsync.discovery.chat_completion", return_value=ai_response),
            patch("dotsync.discovery._load_classification_cache", return_value={}),
            patch("dotsync.discovery._save_classification_cache"),
        ):
            result = classify_with_ai([cf], cfg)

        assert result[0].include is True
        assert result[0].reason == "ai:include"

    def test_ai_classify_fallback_on_error(self, tmp_path: Path) -> None:
        cfg = _default_cfg(llm_endpoint="http://localhost:8000")
        cf = self._make_candidate(tmp_path, "myapp.conf")

        with (
            patch("dotsync.discovery.chat_completion", side_effect=LLMError("refused")),
            patch("dotsync.discovery._load_classification_cache", return_value={}),
            patch("dotsync.discovery._save_classification_cache"),
        ):
            result = classify_with_ai([cf], cfg)

        assert result[0].include is None
        assert result[0].reason == "ask_user"

    def test_ai_classify_uses_cache(self, tmp_path: Path) -> None:
        cfg = _default_cfg(llm_endpoint="http://localhost:8000")
        cf = self._make_candidate(tmp_path, "cached.conf")

        cache = {"cached.conf": {"include": True, "reason": "ai:include"}}

        with (
            patch("dotsync.discovery._load_classification_cache", return_value=cache),
            patch("dotsync.discovery._save_classification_cache"),
            patch("dotsync.discovery.chat_completion") as mock_chat,
        ):
            result = classify_with_ai([cf], cfg)

        mock_chat.assert_not_called()
        assert result[0].include is True
        assert result[0].reason == "ai:include"

    def test_ai_classify_saves_to_cache(self, tmp_path: Path) -> None:
        cfg = _default_cfg(llm_endpoint="http://localhost:8000")
        cf = self._make_candidate(tmp_path, "newfile.conf")

        ai_response = json.dumps([{"path": "newfile.conf", "verdict": "exclude"}])

        saved_cache: dict = {}

        def capture_save(cache: dict) -> None:
            saved_cache.update(cache)

        with (
            patch("dotsync.discovery.chat_completion", return_value=ai_response),
            patch("dotsync.discovery._load_classification_cache", return_value={}),
            patch("dotsync.discovery._save_classification_cache", side_effect=capture_save),
        ):
            classify_with_ai([cf], cfg)

        assert "newfile.conf" in saved_cache
        assert saved_cache["newfile.conf"]["include"] is False
        assert saved_cache["newfile.conf"]["reason"] == "ai:exclude"


# ---------------------------------------------------------------------------
# Step 2.6 — discover orchestrator
# ---------------------------------------------------------------------------


class TestDiscover:
    def test_discover_returns_config_file_list(self, tmp_path: Path) -> None:
        _make_file(tmp_path, ".bashrc", "# bash")
        _make_file(tmp_path, "random.txt", "stuff")

        cfg = _default_cfg()

        with (
            patch("dotsync.discovery.config_dirs", return_value=[tmp_path]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            result = discover(cfg)

        assert isinstance(result, list)
        assert all(isinstance(cf, ConfigFile) for cf in result)
        assert len(result) >= 1

        # .bashrc should be known/included
        bashrc = [cf for cf in result if cf.path == Path(".bashrc")]
        assert len(bashrc) == 1
        assert bashrc[0].include is True

    def test_discover_skips_ai_when_no_endpoint(self, tmp_path: Path) -> None:
        _make_file(tmp_path, "unknown.conf", "stuff")

        cfg = _default_cfg(llm_endpoint=None)

        with (
            patch("dotsync.discovery.config_dirs", return_value=[tmp_path]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
            patch("dotsync.discovery.classify_with_ai") as mock_ai,
        ):
            result = discover(cfg)

        mock_ai.assert_not_called()

        # Unknown files should get reason "ask_user"
        unknowns = [cf for cf in result if cf.path == Path("unknown.conf")]
        assert len(unknowns) == 1
        assert unknowns[0].include is None
        assert unknowns[0].reason == "ask_user"

    def test_discover_never_returns_reason_unknown(self, tmp_path: Path) -> None:
        """Every file from discover() must be resolved — no reason='unknown'."""
        _make_file(tmp_path, ".bashrc", "# bash")
        _make_file(tmp_path, "mystery.conf", "stuff")
        _make_file(tmp_path, "another.txt", "data")

        cfg = _default_cfg(llm_endpoint=None)

        with (
            patch("dotsync.discovery.config_dirs", return_value=[tmp_path]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            result = discover(cfg)

        for cf in result:
            assert cf.reason != "unknown", (
                f"{cf.path} has reason='unknown' — discover() must resolve all files"
            )

    def test_discover_excludes_ssh_private_keys_end_to_end(self, tmp_path: Path) -> None:
        """SSH private keys must never appear in discover() output."""
        _make_file(tmp_path, ".ssh/id_rsa", "secret key")
        _make_file(tmp_path, ".ssh/id_ed25519", "another secret")
        _make_file(tmp_path, ".ssh/id_rsa.pub", "public key")
        _make_file(tmp_path, ".ssh/config", "Host *")

        cfg = _default_cfg()

        with (
            patch("dotsync.discovery.config_dirs", return_value=[tmp_path]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            result = discover(cfg)

        paths = {str(cf.path) for cf in result}
        assert ".ssh/id_rsa" not in paths
        assert ".ssh/id_ed25519" not in paths

    def test_discover_ai_only_receives_unknowns(self, tmp_path: Path) -> None:
        """When AI is called, it should only receive files with include=None."""
        _make_file(tmp_path, ".bashrc", "# known file")
        _make_file(tmp_path, "ambiguous.conf", "unknown file")

        cfg = _default_cfg(llm_endpoint="http://localhost:8000")

        ai_received: list[ConfigFile] = []

        def fake_ai(candidates: list[ConfigFile], _cfg: DotSyncConfig) -> list[ConfigFile]:
            ai_received.extend(candidates)
            for cf in candidates:
                cf.include = None
                cf.reason = "ask_user"
            return candidates

        with (
            patch("dotsync.discovery.config_dirs", return_value=[tmp_path]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
            patch("dotsync.discovery.classify_with_ai", side_effect=fake_ai),
        ):
            discover(cfg)

        # AI should only have received unknowns, not .bashrc
        ai_paths = {str(cf.path) for cf in ai_received}
        assert ".bashrc" not in ai_paths
        # All files sent to AI should have had include=None before the call
        # (we verify indirectly: .bashrc is known → include=True, so excluded)
        assert len(ai_received) >= 1


# ---------------------------------------------------------------------------
# Cache persistence integration
# ---------------------------------------------------------------------------


class TestCachePersistence:
    def test_cache_round_trip(self, tmp_path: Path, monkeypatch: object) -> None:
        """Cache written by _save can be read back by _load."""
        import dotsync.discovery as disc

        cache_file = tmp_path / "classification_cache.json"
        monkeypatch.setattr(disc, "CLASSIFICATION_CACHE_FILE", cache_file)  # type: ignore[attr-defined]

        original = {
            "myfile.conf": {"include": True, "reason": "ai:include"},
            "other.conf": {"include": False, "reason": "ai:exclude"},
        }

        _save_classification_cache(original)
        assert cache_file.exists()

        loaded = _load_classification_cache()
        assert loaded == original

    def test_cache_handles_missing_file(self, tmp_path: Path, monkeypatch: object) -> None:
        """_load returns empty dict when cache file doesn't exist."""
        import dotsync.discovery as disc

        cache_file = tmp_path / "nonexistent" / "cache.json"
        monkeypatch.setattr(disc, "CLASSIFICATION_CACHE_FILE", cache_file)  # type: ignore[attr-defined]

        assert _load_classification_cache() == {}

    def test_cache_handles_corrupt_json(self, tmp_path: Path, monkeypatch: object) -> None:
        """_load returns empty dict when cache file contains invalid JSON."""
        import dotsync.discovery as disc

        cache_file = tmp_path / "classification_cache.json"
        cache_file.write_text("not valid json {{{", encoding="utf-8")
        monkeypatch.setattr(disc, "CLASSIFICATION_CACHE_FILE", cache_file)  # type: ignore[attr-defined]

        assert _load_classification_cache() == {}
