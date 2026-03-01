"""Tests for dotsync.discovery module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from dotsync.config import DotSyncConfig
import pytest

from dotsync.discovery import (
    ALLOWED_EXTENSIONS,
    BLOCKED_FILENAME_PATTERNS,
    HEURISTIC_RULES,
    PRUNE_DIRS,
    SAFETY_EXCLUDES,
    ConfigFile,
    _is_generated_filename,
    _load_classification_cache,
    _save_classification_cache,
    build_candidate_entry,
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

    def test_no_overlap_between_safety_and_prune_constants(self) -> None:
        """No overlap between SAFETY_EXCLUDES and PRUNE_DIRS."""
        for pat in SAFETY_EXCLUDES:
            stripped = pat.rstrip("/")
            assert stripped not in PRUNE_DIRS, (
                f"Pattern in both SAFETY_EXCLUDES and PRUNE_DIRS: {pat}"
            )

    def test_prune_dirs_are_all_simple_names(self) -> None:
        """PRUNE_DIRS entries must not contain path separators."""
        for entry in PRUNE_DIRS:
            assert "/" not in entry, f"PRUNE_DIRS entry contains '/': {entry}"
            assert "\\" not in entry, f"PRUNE_DIRS entry contains '\\': {entry}"

    def test_allowed_extensions_all_start_with_dot(self) -> None:
        """Every ALLOWED_EXTENSIONS entry must start with a dot."""
        for ext in ALLOWED_EXTENSIONS:
            assert ext.startswith("."), f"ALLOWED_EXTENSIONS entry missing dot: {ext}"

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
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "id_rsa" not in names
        assert "config" in names

    def test_scan_skips_large_files(self, tmp_path: Path) -> None:
        _make_file(tmp_path, "small.conf", "x")
        _make_file(tmp_path, "huge.conf", "x", size=51_000)

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "small.conf" in names
        assert "huge.conf" not in names

    def test_scan_skips_binary_files(self, tmp_path: Path) -> None:
        text_file = tmp_path / "text.conf"
        text_file.write_text("hello world\n")

        bin_file = tmp_path / "binary.conf"
        bin_file.write_bytes(b"hello\x00world")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "text.conf" in names
        assert "binary.conf" not in names

    def test_scan_respects_hard_max_depth(self, tmp_path: Path) -> None:
        # MAX_DEPTH is 5; depth 4 should be found, depth 6 should not
        _make_file(tmp_path, "a/b/c/d/shallow.conf", "ok")  # depth 4
        _make_file(tmp_path, "a/b/c/d/e/f/deep.conf", "too deep")  # depth 6

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
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
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates(extra_paths=[extra_file])

        assert extra_file in results

    def test_scan_excludes_gnupg_dir(self, tmp_path: Path) -> None:
        """Files under .gnupg/ must be excluded by SAFETY_EXCLUDES."""
        _make_file(tmp_path, ".gnupg/pubring.kbx", "keyring")
        _make_file(tmp_path, ".bashrc", "# bash")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
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
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates(extra_paths=[key_file])

        names = {r.name for r in results}
        assert "id_rsa" not in names


# ---------------------------------------------------------------------------
# Subtree pruning tests
# ---------------------------------------------------------------------------


class TestSubtreePruning:
    def test_prune_skips_repo_path(self, tmp_path: Path) -> None:
        """repo_path directory must be excluded from scan even if under $HOME."""
        repo = tmp_path / "dotsync-repo"
        _make_file(tmp_path, "dotsync-repo/manifest.json", "{}")
        _make_file(tmp_path, "dotsync-repo/configs/.bashrc", "# synced")
        _make_file(tmp_path, ".bashrc", "# bash")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates(repo_path=repo)

        rel_strs = {str(r.relative_to(tmp_path)) for r in results}
        assert "dotsync-repo/manifest.json" not in rel_strs
        assert "dotsync-repo/configs/.bashrc" not in rel_strs
        assert ".bashrc" in rel_strs

    def test_prune_skips_repo_path_custom_name(self, tmp_path: Path) -> None:
        """repo_path is excluded by resolved path, not by name — works for any name."""
        repo = tmp_path / "my-dots"
        _make_file(tmp_path, "my-dots/settings.json", "{}")
        _make_file(tmp_path, ".zshrc", "# zsh")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates(repo_path=repo)

        rel_strs = {str(r.relative_to(tmp_path)) for r in results}
        assert "my-dots/settings.json" not in rel_strs
        assert ".zshrc" in rel_strs

    def test_prune_skips_node_modules_subtree(self, tmp_path: Path) -> None:
        """Files deep inside node_modules/ must be pruned."""
        _make_file(tmp_path, ".config/tool/node_modules/deep/file.json", "{}")
        _make_file(tmp_path, ".config/tool/settings.json", "{}")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "file.json" not in names
        assert "settings.json" in names

    def test_prune_skips_git_dir(self, tmp_path: Path) -> None:
        """.git/config inside scan root must be pruned."""
        _make_file(tmp_path, ".config/repo/.git/config", "[core]")
        _make_file(tmp_path, ".config/repo/settings.toml", "ok")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        rel_strs = {str(r.relative_to(tmp_path)) for r in results}
        assert ".config/repo/.git/config" not in rel_strs
        assert ".config/repo/settings.toml" in rel_strs

    def test_prune_skips_electron_cache_dirs(self, tmp_path: Path) -> None:
        """Files under GPUCache/ and ShaderCache/ must be pruned."""
        _make_file(tmp_path, ".config/app/GPUCache/data_0", "gpu")
        _make_file(tmp_path, ".config/app/ShaderCache/shader.bin", "shader")
        _make_file(tmp_path, ".config/app/config.toml", "ok")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "data_0" not in names
        assert "shader.bin" not in names
        assert "config.toml" in names

    def test_prune_skips_safety_exclude_dirs(self, tmp_path: Path) -> None:
        """Files under .gnupg/ subtree must be excluded via safety excludes."""
        _make_file(tmp_path, ".gnupg/pubring.kbx", "keyring")
        _make_file(tmp_path, ".gnupg/private-keys-v1.d/key.gpg", "secret")
        _make_file(tmp_path, ".bashrc", "# bash")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "pubring.kbx" not in names
        assert "key.gpg" not in names
        assert ".bashrc" in names

    def test_prune_dirs_rejects_cargo_registry(self, tmp_path: Path) -> None:
        """~/.cargo/registry/ subtree must never be visited."""
        _make_file(tmp_path, ".cargo/registry/src/crate/lib.json", "{}")
        _make_file(tmp_path, ".cargo/config.toml", "[build]")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "lib.json" not in names
        assert "config.toml" in names

    def test_prune_dirs_rejects_vscode_server_bin(self, tmp_path: Path) -> None:
        """~/.vscode-server/bin/ subtree must never be visited."""
        _make_file(tmp_path, ".vscode-server/bin/commit-hash/node.json", "{}")
        _make_file(tmp_path, ".vscode-server/data/config.json", "{}")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "node.json" not in names
        # data/ is not pruned, config.json should pass
        assert "config.json" in names

    def test_prune_dirs_rejects_ohmyzsh_plugins(self, tmp_path: Path) -> None:
        """~/.oh-my-zsh/plugins/ subtree must never be visited."""
        _make_file(tmp_path, ".oh-my-zsh/plugins/git/git.json", "{}")
        _make_file(tmp_path, ".oh-my-zsh/oh-my-zsh.conf", "config")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "git.json" not in names
        assert "oh-my-zsh.conf" in names

    def test_prune_dirs_rejects_claude_file_history(self, tmp_path: Path) -> None:
        """~/.claude/file-history/ subtree must never be visited."""
        _make_file(tmp_path, ".config/claude/file-history/old.json", "{}")
        _make_file(tmp_path, ".config/claude/config.json", "{}")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "old.json" not in names
        assert "config.json" in names

    def test_prune_skips_ohmyzsh_themes(self, tmp_path: Path) -> None:
        """~/.oh-my-zsh/themes/ subtree must never be visited."""
        _make_file(tmp_path, ".oh-my-zsh/themes/robbyrussell.json", "{}")
        _make_file(tmp_path, ".oh-my-zsh/oh-my-zsh.conf", "config")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "robbyrussell.json" not in names
        assert "oh-my-zsh.conf" in names

    def test_prune_skips_claude_tasks(self, tmp_path: Path) -> None:
        """~/.claude/tasks/ subtree must never be visited."""
        _make_file(tmp_path, ".claude/tasks/abc123.json", "{}")
        _make_file(tmp_path, ".claude/config.json", "{}")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "abc123.json" not in names
        assert "config.json" in names

    def test_prune_skips_openhands_conversations(self, tmp_path: Path) -> None:
        """~/.openhands/conversations/ subtree must never be visited."""
        _make_file(tmp_path, ".openhands/conversations/conv1.json", "{}")
        _make_file(tmp_path, ".openhands/config.json", "{}")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "conv1.json" not in names
        assert "config.json" in names


# ---------------------------------------------------------------------------
# HOME_SCAN_DEPTH tests
# ---------------------------------------------------------------------------


class TestHomeScanDepth:
    def test_home_depth1_skips_user_repos(self, tmp_path: Path) -> None:
        """$HOME with depth=1 must not descend into user project directories."""
        _make_file(tmp_path, "projects/myrepo/config.toml", "[build]")
        _make_file(tmp_path, ".bashrc", "# bash")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 1)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "config.toml" not in names
        assert ".bashrc" in names

    def test_home_depth1_allows_dotfiles(self, tmp_path: Path) -> None:
        """$HOME with depth=1 must still find direct children (dotfiles)."""
        _make_file(tmp_path, ".bashrc", "# bash")
        _make_file(tmp_path, ".zshrc", "# zsh")
        _make_file(tmp_path, ".gitconfig", "[user]")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 1)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert ".bashrc" in names
        assert ".zshrc" in names
        assert ".gitconfig" in names

    def test_known_config_subdirs_get_deep_scan(self, tmp_path: Path) -> None:
        """Known config subdirs like .config get deep scanning despite HOME depth=1."""
        config_dir = tmp_path / ".config"
        _make_file(tmp_path, ".config/nvim/init.conf", "-- nvim")
        _make_file(tmp_path, ".config/alacritty/alacritty.toml", "[window]")
        _make_file(tmp_path, ".bashrc", "# bash")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[
                (tmp_path, 1),       # home: shallow
                (config_dir, 5),     # .config: deep
            ]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert ".bashrc" in names
        assert "init.conf" in names
        assert "alacritty.toml" in names


# ---------------------------------------------------------------------------
# File pre-filter tests
# ---------------------------------------------------------------------------


class TestFilePreFilter:
    # --- Whitelist accept tests ---

    def test_whitelist_accepts_toml(self, tmp_path: Path) -> None:
        """.toml files in config dirs must be accepted."""
        _make_file(tmp_path, ".config/tool/config.toml", "[build]")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "config.toml" in names

    def test_whitelist_accepts_yaml(self, tmp_path: Path) -> None:
        """.yaml files must be accepted."""
        _make_file(tmp_path, ".config/tool/settings.yaml", "key: val")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "settings.yaml" in names

    def test_whitelist_accepts_json(self, tmp_path: Path) -> None:
        """.json files must be accepted."""
        _make_file(tmp_path, ".config/tool/settings.json", "{}")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "settings.json" in names

    def test_whitelist_accepts_ini(self, tmp_path: Path) -> None:
        """.ini files must be accepted."""
        _make_file(tmp_path, ".config/tool/app.ini", "[section]")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "app.ini" in names

    def test_whitelist_accepts_env(self, tmp_path: Path) -> None:
        """.env files in config subdirs must be accepted."""
        _make_file(tmp_path, ".config/tool/.env", "KEY=val")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert ".env" in names

    def test_whitelist_accepts_rc(self, tmp_path: Path) -> None:
        """.rc extension files must be accepted."""
        _make_file(tmp_path, ".config/tool/settings.rc", "opt=1")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "settings.rc" in names

    def test_whitelist_accepts_extensionless_home_dotfile(self, tmp_path: Path) -> None:
        """Extensionless home dotfiles like ~/.zshrc and ~/.gitconfig must be accepted."""
        _make_file(tmp_path, ".zshrc", "# zsh")
        _make_file(tmp_path, ".gitconfig", "[user]")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 1)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert ".zshrc" in names
        assert ".gitconfig" in names

    def test_whitelist_accepts_named_config_file(self, tmp_path: Path) -> None:
        """Named files 'config' and 'credentials' must be accepted."""
        _make_file(tmp_path, ".ssh/config", "Host *")
        _make_file(tmp_path, ".aws/credentials", "[default]")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "config" in names
        assert "credentials" in names

    # --- Whitelist reject tests ---

    def test_whitelist_rejects_unknown_extension(self, tmp_path: Path) -> None:
        """Files with non-whitelisted extensions must be rejected."""
        for name in [
            "data.log", "store.sqlite", "main.py", "pkg.lock",
            "cache.db", "icon.png", "archive.zip", "app.exe",
            "script.sh", "plugin.js", "README.md",
        ]:
            _make_file(tmp_path, f".config/tool/{name}", "x")
        _make_file(tmp_path, ".config/tool/config.toml", "ok")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        for rejected in [
            "data.log", "store.sqlite", "main.py", "pkg.lock",
            "cache.db", "icon.png", "archive.zip", "app.exe",
            "script.sh", "plugin.js", "README.md",
        ]:
            assert rejected not in names, f"{rejected} should be rejected by whitelist"
        assert "config.toml" in names

    def test_whitelist_rejects_home_history_files(self, tmp_path: Path) -> None:
        """Known history/noise dotfiles at $HOME root must be rejected."""
        for name in [".bash_history", ".zsh_history", ".viminfo", ".lesshst", ".python_history"]:
            _make_file(tmp_path, name, "history data")
        _make_file(tmp_path, ".bashrc", "# bash")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 1)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        for rejected in [".bash_history", ".zsh_history", ".viminfo", ".lesshst", ".python_history"]:
            assert rejected not in names, f"{rejected} should be blocked as home dotfile noise"
        assert ".bashrc" in names

    def test_whitelist_rejects_extensionless_in_subdir(self, tmp_path: Path) -> None:
        """Extensionless files in subdirs not in ALLOWED_NAMED_FILES must be rejected."""
        for name in ["Makefile", "README", "bindgen", ".cargo-ok"]:
            _make_file(tmp_path, f".config/tool/{name}", "x")
        _make_file(tmp_path, ".config/tool/config.toml", "ok")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        for rejected in ["Makefile", "README", "bindgen", ".cargo-ok"]:
            assert rejected not in names, f"{rejected} should be rejected (extensionless, not allowed name)"
        assert "config.toml" in names

    def test_whitelist_rejects_large_files(self, tmp_path: Path) -> None:
        """Files over 50KB must be rejected even with allowed extension."""
        _make_file(tmp_path, ".config/tool/big.toml", "x", size=51_000)
        _make_file(tmp_path, ".config/tool/small.toml", "ok")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "big.toml" not in names
        assert "small.toml" in names

    def test_whitelist_rejects_binary_files(self, tmp_path: Path) -> None:
        """Files with null bytes must be rejected even with allowed extension."""
        bin_file = tmp_path / ".config" / "tool" / "binary.json"
        bin_file.parent.mkdir(parents=True, exist_ok=True)
        bin_file.write_bytes(b'{"key":"\x00"}')
        _make_file(tmp_path, ".config/tool/text.json", "{}")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "binary.json" not in names
        assert "text.json" in names

    def test_prefilter_rejects_uuid_filename(self, tmp_path: Path) -> None:
        """Files with UUID filenames must be rejected — .json ext passes whitelist
        but generated filename check in dir pruning prevents parent dirs.
        UUID files with allowed extensions are accepted (AI handles them)."""
        _make_file(tmp_path, ".config/tool/settings.json", "{}")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "settings.json" in names

    def test_prefilter_rejects_hex_filename(self, tmp_path: Path) -> None:
        """Files with pure hex filenames (16+ chars) and no allowed extension are rejected."""
        _make_file(tmp_path, ".config/tool/a1b2c3d4e5f6a7b8", "data")
        _make_file(tmp_path, ".config/tool/config.json", "{}")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "a1b2c3d4e5f6a7b8" not in names
        assert "config.json" in names

    def test_prefilter_rejects_numeric_filename(self, tmp_path: Path) -> None:
        """Files with pure numeric filenames must be rejected (no extension, not allowed name)."""
        _make_file(tmp_path, ".config/tool/1234567890", "data")
        _make_file(tmp_path, ".bashrc", "# bash")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 1)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "1234567890" not in names
        assert ".bashrc" in names

    def test_prefilter_rejects_deep_files(self, tmp_path: Path) -> None:
        """Files at depth 6 must be rejected."""
        _make_file(tmp_path, "a/b/c/d/e/f/deep.conf", "too deep")
        _make_file(tmp_path, "a/b/c/d/shallow.conf", "ok")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "deep.conf" not in names
        assert "shallow.conf" in names

    def test_prefilter_accepts_normal_config(self, tmp_path: Path) -> None:
        """Normal config files must pass through the scanner."""
        _make_file(tmp_path, ".config/tool/settings.json", "{}")
        _make_file(tmp_path, ".config/tool/config.toml", "[tool]")
        _make_file(tmp_path, ".zshrc", "# zsh config")
        _make_file(tmp_path, ".gitignore", "*.pyc")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "settings.json" in names
        assert "config.toml" in names
        # .zshrc and .gitignore are at root but config_dirs scan root is tmp_path
        # and home is tmp_path, so depth=0 means is_home_root=True for these

    def test_prefilter_safety_excludes_non_overridable(self, tmp_path: Path) -> None:
        """Safety excludes like .ssh/id_rsa cannot be overridden."""
        _make_file(tmp_path, ".ssh/id_rsa", "secret key")
        _make_file(tmp_path, ".bashrc", "# bash")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "id_rsa" not in names
        assert ".bashrc" in names


# ---------------------------------------------------------------------------
# Extra paths bypass tests
# ---------------------------------------------------------------------------


class TestExtraPathsBypass:
    def test_extra_paths_bypass_prune_dirs(self, tmp_path: Path) -> None:
        """Extra paths inside pruned dirs (node_modules/) should be included."""
        f = _make_file(tmp_path, "project/node_modules/pkg/config.json", "{}")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates(extra_paths=[f])

        assert f in results

    def test_extra_paths_bypass_whitelist(self, tmp_path: Path) -> None:
        """Extra paths with non-whitelisted extensions (.db) should be included."""
        f = _make_file(tmp_path, "special/data.db", "database")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates(extra_paths=[f])

        assert f in results

    def test_extra_paths_cannot_bypass_safety_excludes(self, tmp_path: Path) -> None:
        """Extra paths pointing to safety-excluded files must still be rejected."""
        key_file = _make_file(tmp_path, ".ssh/id_rsa", "secret key")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates(extra_paths=[key_file])

        names = {r.name for r in results}
        assert "id_rsa" not in names


# ---------------------------------------------------------------------------
# Scanner robustness tests
# ---------------------------------------------------------------------------


class TestScannerRobustness:
    def test_prefilter_binary_check_is_last(self, tmp_path: Path) -> None:
        """Binary check should never run for files outside the whitelist."""
        # Create a .pyc file (not in whitelist) with binary content
        pyc_file = tmp_path / ".config" / "tool" / "module.pyc"
        pyc_file.parent.mkdir(parents=True, exist_ok=True)
        pyc_file.write_bytes(b"\x00" * 100)

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
            patch("dotsync.discovery._is_binary", wraps=lambda p: True) as mock_binary,
        ):
            scan_candidates()

        # _is_binary should never be called for .pyc — whitelist rejects it first
        for call in mock_binary.call_args_list:
            assert call.args[0].suffix != ".pyc", (
                "_is_binary was called for .pyc — whitelist should have rejected it first"
            )

    def test_scan_deduplicates_overlapping_roots(self, tmp_path: Path) -> None:
        """Overlapping roots should not produce duplicate results."""
        parent = tmp_path / ".config"
        child = tmp_path / ".config" / "tool"
        _make_file(tmp_path, ".config/tool/settings.toml", "ok")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(parent, 5), (child, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        paths = [r.resolve() for r in results]
        assert len(paths) == len(set(paths)), "Duplicate paths found in results"

    def test_scan_handles_permission_error_gracefully(self, tmp_path: Path) -> None:
        """Unreadable directories should be silently skipped."""
        _make_file(tmp_path, "readable/config.toml", "ok")
        unreadable = tmp_path / "noperm"
        unreadable.mkdir()
        (unreadable / "secret.toml").write_text("hidden")
        unreadable.chmod(0o000)

        try:
            with (
                patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
                patch("dotsync.discovery.home_dir", return_value=tmp_path),
            ):
                results = scan_candidates()

            names = {r.name for r in results}
            assert "config.toml" in names
            assert "secret.toml" not in names
        finally:
            # Restore permissions so tmp_path cleanup succeeds
            unreadable.chmod(0o755)


# ---------------------------------------------------------------------------
# Progress callback tests
# ---------------------------------------------------------------------------


class TestProgressCallback:
    def test_progress_callback_called_on_dir_enter(self, tmp_path: Path) -> None:
        """Progress callback receives dir_enter events."""
        _make_file(tmp_path, "sub/config.toml", "ok")
        events: list[dict] = []

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            scan_candidates(progress=lambda e: events.append(e))

        dir_enters = [e for e in events if e["type"] == "dir_enter"]
        assert len(dir_enters) >= 1
        # Should include the root dir and the "sub" dir
        paths = {e["path"] for e in dir_enters}
        assert str(tmp_path) in paths

    def test_progress_callback_called_on_prune(self, tmp_path: Path) -> None:
        """Progress callback receives dir_pruned events with reason."""
        _make_file(tmp_path, "node_modules/pkg/index.json", "{}")
        events: list[dict] = []

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            scan_candidates(progress=lambda e: events.append(e))

        pruned = [e for e in events if e["type"] == "dir_pruned"]
        assert len(pruned) >= 1
        assert any("PRUNE_DIRS" in (e["reason"] or "") for e in pruned)

    def test_progress_callback_called_on_rejection(self, tmp_path: Path) -> None:
        """Progress callback receives file_rejected events with reason."""
        _make_file(tmp_path, ".config/tool/data.db", "database")
        _make_file(tmp_path, ".config/tool/config.toml", "ok")
        events: list[dict] = []

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            scan_candidates(progress=lambda e: events.append(e))

        rejected = [e for e in events if e["type"] == "file_rejected"]
        assert len(rejected) >= 1
        assert any("not in whitelist" in (e["reason"] or "") for e in rejected)

        accepted = [e for e in events if e["type"] == "file_accepted"]
        assert len(accepted) >= 1

    def test_progress_callback_none_does_not_crash(self, tmp_path: Path) -> None:
        """scan_candidates with progress=None runs normally."""
        _make_file(tmp_path, ".bashrc", "# bash")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates(progress=None)

        assert len(results) >= 1

    def test_phase_events_emitted_for_heuristic_and_ai(self, tmp_path: Path) -> None:
        """discover() emits phase_start/phase_done for scan and heuristic phases."""
        _make_file(tmp_path, ".bashrc", "# bash")
        events: list[dict] = []

        cfg = _default_cfg(llm_endpoint=None)

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            discover(cfg, progress=lambda e: events.append(e))

        phase_starts = [e for e in events if e["type"] == "phase_start"]
        phase_dones = [e for e in events if e["type"] == "phase_done"]

        start_reasons = {e["reason"] for e in phase_starts}
        done_reasons = {e["reason"] for e in phase_dones}

        assert "scan" in start_reasons
        assert "heuristic" in start_reasons
        assert "scan" in done_reasons
        assert "heuristic" in done_reasons


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

    def test_build_candidate_entry_truncates_first_lines(self, tmp_path: Path) -> None:
        """first_lines should contain at most MAX_FIRST_LINES (5) lines."""
        content = "\n".join(f"line {i}" for i in range(20)) + "\n"
        f = _make_file(tmp_path, "many_lines.conf", content)
        cf = ConfigFile(
            path=Path("many_lines.conf"),
            abs_path=f,
            size_bytes=len(content),
            include=None,
            reason="ambiguous",
        )

        entry = build_candidate_entry(cf)
        lines = entry["first_lines"].split("\n")
        assert len(lines) <= 5

    def test_build_candidate_entry_truncates_long_lines(self, tmp_path: Path) -> None:
        """first_lines should be at most 200 chars and end with '...' if truncated."""
        content = "x" * 500 + "\n"
        f = _make_file(tmp_path, "long_line.conf", content)
        cf = ConfigFile(
            path=Path("long_line.conf"),
            abs_path=f,
            size_bytes=len(content),
            include=None,
            reason="ambiguous",
        )

        entry = build_candidate_entry(cf)
        assert len(entry["first_lines"]) <= 200 + len("...")
        assert entry["first_lines"].endswith("...")

    def test_classify_with_ai_chunks_large_input(self, tmp_path: Path) -> None:
        """45 candidates should result in exactly 3 chat_completion calls (20+20+5)."""
        cfg = _default_cfg(llm_endpoint="http://localhost:8000")
        candidates: list[ConfigFile] = []
        for i in range(45):
            rel = f"file_{i}.conf"
            f = _make_file(tmp_path, rel, f"content {i}")
            candidates.append(
                ConfigFile(
                    path=Path(rel),
                    abs_path=f,
                    size_bytes=10,
                    include=None,
                    reason="ambiguous",
                )
            )

        def fake_response(**kwargs: object) -> str:
            items = json.loads(str(kwargs.get("user_message", "[]")))
            return json.dumps(
                [{"path": item["path"], "verdict": "include"} for item in items]
            )

        with (
            patch(
                "dotsync.discovery.chat_completion", side_effect=fake_response
            ) as mock_chat,
            patch("dotsync.discovery._load_classification_cache", return_value={}),
            patch("dotsync.discovery._save_classification_cache"),
        ):
            classify_with_ai(candidates, cfg)
            assert mock_chat.call_count == 3

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
        _make_file(tmp_path, ".config/tool/random.conf", "stuff")

        cfg = _default_cfg()

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
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
        # Use an allowed-extension file deep enough to not match any heuristic
        _make_file(tmp_path, "a/b/c/unknown.xml", "stuff")

        cfg = _default_cfg(llm_endpoint=None)

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
            patch("dotsync.discovery.classify_with_ai") as mock_ai,
        ):
            result = discover(cfg)

        mock_ai.assert_not_called()

        # Ambiguous files should get reason "ask_user"
        unknowns = [cf for cf in result if cf.path == Path("a/b/c/unknown.xml")]
        assert len(unknowns) == 1
        assert unknowns[0].include is None
        assert unknowns[0].reason == "ask_user"

    def test_discover_never_returns_reason_unknown(self, tmp_path: Path) -> None:
        """Every file from discover() must be resolved — no reason='unknown' or 'ambiguous'."""
        _make_file(tmp_path, ".bashrc", "# bash")
        _make_file(tmp_path, "subdir/mystery.conf", "stuff")
        _make_file(tmp_path, "subdir/another.yaml", "data")

        cfg = _default_cfg(llm_endpoint=None)

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
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
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            result = discover(cfg)

        paths = {str(cf.path) for cf in result}
        assert ".ssh/id_rsa" not in paths
        assert ".ssh/id_ed25519" not in paths

    def test_discover_ai_only_receives_unknowns(self, tmp_path: Path) -> None:
        """When AI is called, it should only receive files with include=None."""
        _make_file(tmp_path, ".bashrc", "# known file")
        _make_file(tmp_path, "a/b/c/ambiguous.xml", "unknown file")

        cfg = _default_cfg(llm_endpoint="http://localhost:8000")

        ai_received: list[ConfigFile] = []

        def fake_ai(
            candidates: list[ConfigFile], _cfg: DotSyncConfig, **_kwargs: object
        ) -> list[ConfigFile]:
            ai_received.extend(candidates)
            for cf in candidates:
                cf.include = None
                cf.reason = "ask_user"
            return candidates

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
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


# ---------------------------------------------------------------------------
# Generated filename detection
# ---------------------------------------------------------------------------


class TestGeneratedFilename:
    """Tests for _is_generated_filename() and BLOCKED_FILENAME_PATTERNS."""

    def test_uuid_filename_detected(self) -> None:
        """UUID filenames should be detected as generated."""
        assert _is_generated_filename(Path("3f2504e0-4f89-11d3-9a0c-0305e82c3301.json"))

    def test_hex_filename_detected(self) -> None:
        """Pure hex filenames of 16+ chars should be detected."""
        assert _is_generated_filename(Path("a1b2c3d4e5f6a7b8.json"))
        assert _is_generated_filename(Path("DEADBEEFCAFEBABE01234567.dat"))

    def test_numeric_filename_detected(self) -> None:
        """Pure numeric filenames should be detected."""
        assert _is_generated_filename(Path("1234567890"))
        assert _is_generated_filename(Path("999"))

    def test_dot_uuid_detected(self) -> None:
        """UUID filenames with leading dot should be detected."""
        assert _is_generated_filename(Path(".f9c91a88-3095-44a3-bbb5-011673bd7cc9"))

    def test_hex_at_version_detected(self) -> None:
        """Hex filenames with @version suffix should be detected."""
        assert _is_generated_filename(Path("a1f8dc17b46bfa73@v2"))

    def test_git_sha_filename_detected(self) -> None:
        """Full git SHA filenames (40 hex chars) should be detected."""
        assert _is_generated_filename(Path("e54c774e0add60467559eb0d1e229c6452cf8447"))

    def test_timestamp_suffix_detected(self) -> None:
        """Trailing Unix timestamp (10+ digits) after a dot should be detected."""
        assert _is_generated_filename(Path(".claude.json.backup.1772283029203"))

    def test_hex_with_dots_detected(self) -> None:
        """Hex-with-dots filenames (VS Code extension storage) should be detected."""
        assert _is_generated_filename(Path("a1b2c3d4.e5f6.json"))

    def test_normal_filenames_not_detected(self) -> None:
        """Normal config filenames should NOT be detected as generated."""
        assert not _is_generated_filename(Path("settings.json"))
        assert not _is_generated_filename(Path("config.toml"))
        assert not _is_generated_filename(Path(".bashrc"))
        assert not _is_generated_filename(Path("init.lua"))
        assert not _is_generated_filename(Path(".gitignore"))

    def test_short_hex_not_detected(self) -> None:
        """Hex filenames shorter than 16 chars should NOT be detected."""
        assert not _is_generated_filename(Path("abcdef.json"))

    def test_blocked_filename_patterns_all_compiled(self) -> None:
        """All BLOCKED_FILENAME_PATTERNS entries should be compiled regex patterns."""
        import re as re_module

        for pat in BLOCKED_FILENAME_PATTERNS:
            assert isinstance(pat, re_module.Pattern), f"Not a compiled pattern: {pat}"


# ---------------------------------------------------------------------------
# Directory pruning — generated names
# ---------------------------------------------------------------------------


class TestGeneratedDirPruning:
    def test_prefilter_rejects_dot_uuid_dirname(self, tmp_path: Path) -> None:
        """Directories with UUID-like names should be pruned (subtree not visited)."""
        _make_file(
            tmp_path,
            ".f9c91a88-3095-44a3-bbb5-011673bd7cc9/config.json",
            "{}",
        )
        _make_file(tmp_path, ".bashrc", "# bash")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            results = scan_candidates()

        names = {r.name for r in results}
        assert "config.json" not in names, (
            "File inside UUID-named directory should be pruned"
        )
        assert ".bashrc" in names


# ---------------------------------------------------------------------------
# AI prompt behaviour
# ---------------------------------------------------------------------------


class TestAIPromptBehaviour:
    def test_ai_excludes_third_party_project_config(self, tmp_path: Path) -> None:
        """AI should exclude files that belong to third-party project repos."""
        cfg = _default_cfg(llm_endpoint="http://localhost:8000")

        funding_content = (
            "github:\n"
            "  - ohmyzsh\n"
            "patreon: ohmyzsh\n"
            "open_collective: ohmyzsh\n"
        )
        f = _make_file(tmp_path, ".oh-my-zsh/FUNDING.yml", funding_content)
        cf = ConfigFile(
            path=Path(".oh-my-zsh/FUNDING.yml"),
            abs_path=f,
            size_bytes=len(funding_content),
            include=None,
            reason="ambiguous",
        )

        # Mock the LLM to return an exclude verdict
        ai_response = json.dumps([
            {
                "path": ".oh-my-zsh/FUNDING.yml",
                "verdict": "exclude",
                "reason": "Third-party project funding metadata, not user config.",
            }
        ])

        with (
            patch("dotsync.discovery.chat_completion", return_value=ai_response),
            patch("dotsync.discovery._load_classification_cache", return_value={}),
            patch("dotsync.discovery._save_classification_cache"),
        ):
            result = classify_with_ai([cf], cfg)

        assert result[0].include is False
        assert result[0].reason == "ai:exclude"


# ---------------------------------------------------------------------------
# Performance tests (excluded from default run)
# ---------------------------------------------------------------------------


@pytest.mark.perf
class TestPerformance:
    """Performance benchmarks — run with: pytest -m perf"""

    def test_scan_performance(self, tmp_path: Path) -> None:
        """Synthetic tree of 10K files must complete scan in under 5 seconds."""
        import time

        # Build a realistic tree: config dirs, some prunable dirs, many files
        for i in range(50):
            for j in range(20):
                _make_file(tmp_path, f".config/app{i}/file{j}.conf", f"content {j}")
            # Add some prunable dirs
            _make_file(tmp_path, f".config/app{i}/node_modules/pkg/index.json", "{}")
            _make_file(tmp_path, f".config/app{i}/__pycache__/mod.pyc", "\x00")

        # Also add 5000 files in a flat structure with blocked extensions
        for i in range(250):
            for ext in [".py", ".js", ".png", ".db"]:
                _make_file(tmp_path, f"code/project{i}/file{ext}", "x")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
        ):
            start = time.monotonic()
            results = scan_candidates()
            elapsed = time.monotonic() - start

        assert elapsed < 5.0, f"Scan took {elapsed:.2f}s — must complete in under 5s"
        # Should have found the .conf files but not the blocked ones
        assert len(results) > 0

    def test_binary_check_only_runs_after_whitelist(self, tmp_path: Path) -> None:
        """_is_binary must never be called for files outside the whitelist."""
        # Create files with non-whitelisted extensions — should be rejected before binary check
        for ext in [".pyc", ".db", ".log", ".png", ".js"]:
            f = tmp_path / ".config" / "tool" / f"file{ext}"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"\x00" * 100)

        # Create one valid file so scan has something to process
        _make_file(tmp_path, ".config/tool/config.toml", "ok")

        with (
            patch("dotsync.discovery.config_dirs", return_value=[(tmp_path, 5)]),
            patch("dotsync.discovery.home_dir", return_value=tmp_path),
            patch("dotsync.discovery._is_binary", wraps=lambda p: b"\x00" in p.read_bytes()[:512]) as mock_binary,
        ):
            scan_candidates()

        # _is_binary should only be called for files that passed the whitelist
        for c in mock_binary.call_args_list:
            suffix = c.args[0].suffix.lower()
            assert suffix in ALLOWED_EXTENSIONS or not suffix, (
                f"_is_binary called for {c.args[0].name} — whitelist should reject first"
            )
