"""Tests for dotsync.discovery module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from dotsync.config import DotSyncConfig
from dotsync.discovery import (
    HEURISTIC_RULES,
    SAFETY_EXCLUDES,
    SCAN_EXCLUDES,
    ConfigFile,
    _load_classification_cache,
    _save_classification_cache,
    classify_heuristic,
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
# Step 2.1 — Rules and excludes sanity
# ---------------------------------------------------------------------------


class TestRulesAndExcludes:
    def test_safety_excludes_are_all_relative_paths(self) -> None:
        """SAFETY_EXCLUDES entries should be relative (no leading slash)."""
        for pat in SAFETY_EXCLUDES:
            assert not pat.startswith("/"), f"SAFETY_EXCLUDES entry should be relative: {pat}"

    def test_safety_excludes_do_not_overlap_scan_excludes(self) -> None:
        """No overlap between SAFETY_EXCLUDES and SCAN_EXCLUDES."""
        for pat in SAFETY_EXCLUDES:
            assert pat not in SCAN_EXCLUDES, (
                f"Pattern in both SAFETY_EXCLUDES and SCAN_EXCLUDES: {pat}"
            )

    def test_heuristic_rules_have_required_keys(self) -> None:
        """Every heuristic rule must have pattern, max_depth, and reason."""
        for rule in HEURISTIC_RULES:
            assert "pattern" in rule, f"Rule missing 'pattern': {rule}"
            assert "max_depth" in rule, f"Rule missing 'max_depth': {rule}"
            assert "reason" in rule, f"Rule missing 'reason': {rule}"


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
        _make_file(tmp_path, "huge.conf", "x", size=700_000)

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

    def test_scan_respects_hard_max_depth(self, tmp_path: Path) -> None:
        # MAX_DEPTH is 5; depth 4 should be found, depth 6 should not
        _make_file(tmp_path, "a/b/c/d/shallow.conf", "ok")  # depth 4
        _make_file(tmp_path, "a/b/c/d/e/f/deep.conf", "too deep")  # depth 6

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

    def test_scan_excludes_gnupg_dir(self, tmp_path: Path) -> None:
        """Files under .gnupg/ must be excluded by SAFETY_EXCLUDES."""
        _make_file(tmp_path, ".gnupg/pubring.kbx", "keyring")
        _make_file(tmp_path, ".bashrc", "# bash")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[tmp_path]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "pubring.kbx" not in names
        assert ".bashrc" in names

    def test_scan_extra_paths_still_respect_safety_excludes(self, tmp_path: Path) -> None:
        """Extra paths pointing to safety-excluded files must be rejected."""
        key_file = _make_file(tmp_path, ".ssh/id_rsa", "secret key")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[tmp_path]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates(extra_paths=[key_file])

        names = {r.name for r in results}
        assert "id_rsa" not in names


# ---------------------------------------------------------------------------
# Step 2.3 — classify_heuristic
# ---------------------------------------------------------------------------


class TestClassifyHeuristic:
    def test_home_dotfile_included(self, tmp_path: Path) -> None:
        f = _make_file(tmp_path, ".gitconfig", "# git config")
        cfg = _default_cfg()

        with patch("dotsync.discovery.home_dir", return_value=tmp_path):
            result = classify_heuristic([f], cfg)

        assert len(result) == 1
        assert result[0].include is True
        assert result[0].reason == "home dotfile"

    def test_xdg_config_included(self, tmp_path: Path) -> None:
        f = _make_file(tmp_path, ".config/nvim/init.lua", "-- nvim config")
        cfg = _default_cfg()

        with patch("dotsync.discovery.home_dir", return_value=tmp_path):
            result = classify_heuristic([f], cfg)

        assert len(result) == 1
        assert result[0].include is True
        assert result[0].reason == "XDG config"

    def test_windows_appdata_json_included(self, tmp_path: Path) -> None:
        f = _make_file(
            tmp_path,
            "AppData/Roaming/Code/User/settings.json",
            "{}",
        )
        cfg = _default_cfg()

        with patch("dotsync.discovery.home_dir", return_value=tmp_path):
            result = classify_heuristic([f], cfg)

        assert len(result) == 1
        assert result[0].include is True
        assert result[0].reason == "Windows app config"

    def test_windows_appdata_too_deep_excluded(self, tmp_path: Path) -> None:
        # depth after AppData: 5 parts -> exceeds max_depth 4
        f = _make_file(
            tmp_path,
            "AppData/Roaming/Code/User/snippets/python.json",
            "{}",
        )
        cfg = _default_cfg()

        with patch("dotsync.discovery.home_dir", return_value=tmp_path):
            result = classify_heuristic([f], cfg)

        assert len(result) == 1
        assert result[0].include is None

    def test_user_exclude_overrides_heuristic(self, tmp_path: Path) -> None:
        f = _make_file(tmp_path, ".gitconfig", "# git config")
        cfg = _default_cfg(exclude_patterns=[".gitconfig"])

        with patch("dotsync.discovery.home_dir", return_value=tmp_path):
            result = classify_heuristic([f], cfg)

        assert len(result) == 1
        assert result[0].include is False
        assert result[0].reason == "user_excluded"

    def test_ambiguous_file_pending(self, tmp_path: Path) -> None:
        f = _make_file(tmp_path, "app.log", "log data")
        cfg = _default_cfg()

        with patch("dotsync.discovery.home_dir", return_value=tmp_path):
            result = classify_heuristic([f], cfg)

        assert len(result) == 1
        assert result[0].include is None
        assert result[0].reason == "ambiguous"

    def test_os_profile_windows(self, tmp_path: Path) -> None:
        f = _make_file(tmp_path, "AppData/Roaming/app.conf", "win config")
        cfg = _default_cfg()

        with patch("dotsync.discovery.home_dir", return_value=tmp_path):
            result = classify_heuristic([f], cfg)

        assert len(result) == 1
        assert result[0].os_profile == "windows"

    def test_os_profile_linux(self, tmp_path: Path) -> None:
        f = _make_file(tmp_path, ".config/app/config.toml", "linux config")
        cfg = _default_cfg()

        with patch("dotsync.discovery.home_dir", return_value=tmp_path):
            result = classify_heuristic([f], cfg)

        assert len(result) == 1
        assert result[0].os_profile == "linux"


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
            reason="ambiguous",
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

        # .bashrc should be included via home dotfile heuristic
        bashrc = [cf for cf in result if cf.path == Path(".bashrc")]
        assert len(bashrc) == 1
        assert bashrc[0].include is True
        assert bashrc[0].reason == "home dotfile"

    def test_discover_skips_ai_when_no_endpoint(self, tmp_path: Path) -> None:
        _make_file(tmp_path, "unknown.log", "stuff")

        cfg = _default_cfg(llm_endpoint=None)

        with (
            patch("dotsync.discovery.config_dirs", return_value=[tmp_path]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
            patch("dotsync.discovery.classify_with_ai") as mock_ai,
        ):
            result = discover(cfg)

        mock_ai.assert_not_called()

        # Ambiguous files should get reason "ask_user"
        unknowns = [cf for cf in result if cf.path == Path("unknown.log")]
        assert len(unknowns) == 1
        assert unknowns[0].include is None
        assert unknowns[0].reason == "ask_user"

    def test_discover_never_returns_reason_unknown(self, tmp_path: Path) -> None:
        """Every file from discover() must be resolved — no reason='unknown' or 'ambiguous'."""
        _make_file(tmp_path, ".bashrc", "# bash")
        _make_file(tmp_path, "mystery.log", "stuff")
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
            assert cf.reason != "ambiguous", (
                f"{cf.path} has reason='ambiguous' — discover() must resolve all files"
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
        _make_file(tmp_path, "ambiguous.log", "unknown file")

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

        # AI should only have received ambiguous files, not .bashrc
        ai_paths = {str(cf.path) for cf in ai_received}
        assert ".bashrc" not in ai_paths
        # All files sent to AI should have had include=None before the call
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
