"""Tests for dotsync.git_ops module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import git
import pytest

from dotsync.config import DotSyncConfig
from dotsync.git_ops import (
    MANIFEST_FILENAME,
    ManifestEntry,
    MergeConflictError,
    MissingDependencyError,
    NoRemoteConfiguredError,
    add_to_manifest,
    check_dependencies,
    commit_and_push,
    copy_to_repo,
    get_remote,
    init_repo,
    load_manifest,
    pull,
    remove_from_manifest,
    save_manifest,
    set_remote,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path, **overrides: object) -> DotSyncConfig:
    """Return a DotSyncConfig pointing at tmp_path."""
    defaults: dict = {
        "repo_path": tmp_path / "repo",
        "exclude_patterns": [],
        "include_extra": [],
    }
    defaults.update(overrides)
    return DotSyncConfig(**defaults)


def _make_entry(
    rel: str = ".bashrc",
    os_profile: str = "linux",
    added_at: str = "2026-01-01T00:00:00Z",
    sensitive: bool = False,
) -> ManifestEntry:
    """Create a ManifestEntry for testing."""
    return ManifestEntry(
        relative_path=rel,
        os_profile=os_profile,
        added_at=added_at,
        sensitive_flagged=sensitive,
    )


# ---------------------------------------------------------------------------
# Step 4.1 — TestCheckDependencies
# ---------------------------------------------------------------------------


class TestCheckDependencies:
    def test_check_dependencies_passes_when_git_present(self) -> None:
        """No error when git is on PATH."""
        with patch("dotsync.git_ops.shutil.which", return_value="/usr/bin/git"):
            check_dependencies()  # should not raise

    def test_check_dependencies_raises_on_missing_git(self) -> None:
        """MissingDependencyError when git is not found."""
        with patch("dotsync.git_ops.shutil.which", return_value=None), \
             patch("dotsync.git_ops.current_os", return_value="linux"):
            with pytest.raises(MissingDependencyError, match="git not found"):
                check_dependencies()


# ---------------------------------------------------------------------------
# Step 4.2 — TestInitRepo
# ---------------------------------------------------------------------------


class TestInitRepo:
    def test_init_creates_repo(self, tmp_path: Path) -> None:
        """init_repo creates a git repository."""
        cfg = _cfg(tmp_path)
        repo = init_repo(cfg)
        assert (cfg.repo_path / ".git").is_dir()
        assert isinstance(repo, git.Repo)

    def test_init_creates_gitattributes(self, tmp_path: Path) -> None:
        """init_repo writes .gitattributes."""
        cfg = _cfg(tmp_path)
        init_repo(cfg)
        ga = cfg.repo_path / ".gitattributes"
        assert ga.exists()
        content = ga.read_text(encoding="utf-8")
        assert ".gitattributes !filter !diff" in content

    def test_init_gitattributes_not_filtered(self, tmp_path: Path) -> None:
        """.gitattributes itself is excluded from diff filtering."""
        cfg = _cfg(tmp_path)
        init_repo(cfg)
        content = (cfg.repo_path / ".gitattributes").read_text(encoding="utf-8")
        assert ".gitattributes !filter !diff" in content

    def test_init_creates_gitignore(self, tmp_path: Path) -> None:
        """init_repo writes .gitignore that excludes dotsync.key."""
        cfg = _cfg(tmp_path)
        init_repo(cfg)
        gi = cfg.repo_path / ".gitignore"
        assert gi.exists()
        assert "dotsync.key" in gi.read_text(encoding="utf-8")

    def test_gitignore_is_committed(self, tmp_path: Path) -> None:
        """.gitignore is included in the initial commit."""
        cfg = _cfg(tmp_path)
        repo = init_repo(cfg)
        tracked = [item.path for item in repo.head.commit.tree.traverse()]
        assert ".gitignore" in tracked

    def test_init_creates_manifest(self, tmp_path: Path) -> None:
        """init_repo writes an empty manifest file."""
        cfg = _cfg(tmp_path)
        init_repo(cfg)
        manifest = cfg.repo_path / MANIFEST_FILENAME
        assert manifest.exists()
        data = json.loads(manifest.read_text(encoding="utf-8"))
        assert data == []

    def test_init_idempotent(self, tmp_path: Path) -> None:
        """Calling init_repo twice returns the existing repo without error."""
        cfg = _cfg(tmp_path)
        repo1 = init_repo(cfg)
        repo2 = init_repo(cfg)
        assert repo1.working_dir == repo2.working_dir
        # Should still have exactly one commit
        assert len(list(repo2.iter_commits())) == 1


# ---------------------------------------------------------------------------
# Step 4.3 — TestRemote
# ---------------------------------------------------------------------------# Step 4.4 — TestRemote
# ---------------------------------------------------------------------------


class TestRemote:
    def test_set_remote_adds_origin(self, tmp_path: Path) -> None:
        """set_remote creates origin when it doesn't exist."""
        cfg = _cfg(tmp_path)
        repo = init_repo(cfg)
        set_remote(repo, "https://github.com/user/dotfiles.git")
        assert get_remote(repo) == "https://github.com/user/dotfiles.git"

    def test_set_remote_updates_existing_origin(self, tmp_path: Path) -> None:
        """set_remote updates URL when origin already exists."""
        cfg = _cfg(tmp_path)
        repo = init_repo(cfg)
        set_remote(repo, "https://github.com/user/old.git")
        set_remote(repo, "https://github.com/user/new.git")
        assert get_remote(repo) == "https://github.com/user/new.git"

    def test_get_remote_returns_none_when_no_remote(self, tmp_path: Path) -> None:
        """get_remote returns None for a repo with no origin."""
        cfg = _cfg(tmp_path)
        repo = init_repo(cfg)
        assert get_remote(repo) is None


# ---------------------------------------------------------------------------
# Step 4.5 — TestManifest
# ---------------------------------------------------------------------------


class TestManifest:
    def test_manifest_roundtrip(self, tmp_path: Path) -> None:
        """Entries survive save → load round trip."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        entries = [
            _make_entry(".bashrc"),
            _make_entry(".config/nvim/init.lua", os_profile="shared"),
        ]
        save_manifest(repo_path, entries)
        loaded = load_manifest(repo_path)

        assert len(loaded) == 2
        assert loaded[0].relative_path == ".bashrc"
        assert loaded[1].relative_path == ".config/nvim/init.lua"
        assert loaded[1].os_profile == "shared"

    def test_add_to_manifest_deduplicates(self, tmp_path: Path) -> None:
        """Adding the same relative_path twice does not create duplicates."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        entry = _make_entry(".bashrc")
        add_to_manifest(repo_path, entry)
        add_to_manifest(repo_path, entry)

        loaded = load_manifest(repo_path)
        assert len(loaded) == 1

    def test_remove_from_manifest(self, tmp_path: Path) -> None:
        """Removing an entry by relative_path works correctly."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        entries = [_make_entry(".bashrc"), _make_entry(".zshrc")]
        save_manifest(repo_path, entries)
        remove_from_manifest(repo_path, ".bashrc")

        loaded = load_manifest(repo_path)
        assert len(loaded) == 1
        assert loaded[0].relative_path == ".zshrc"


# ---------------------------------------------------------------------------
# Step 4.6 — TestPushPull
# ---------------------------------------------------------------------------


class TestPushPull:
    def test_commit_and_push_stages_all_changes(self, tmp_path: Path) -> None:
        """commit_and_push stages and commits new files."""
        cfg = _cfg(tmp_path)
        repo = init_repo(cfg)

        # Create a new file
        (cfg.repo_path / "test.txt").write_text("hello", encoding="utf-8")

        # Mock push since we have no real remote
        set_remote(repo, "https://github.com/user/dotfiles.git")
        with patch("git.remote.Remote.push"):
            commit_and_push(repo, "test: add file")

        # Should have 2 commits now (init + our commit)
        commits = list(repo.iter_commits())
        assert len(commits) == 2
        assert commits[0].message == "test: add file"

    def test_commit_message_contains_hostname(self, tmp_path: Path) -> None:
        """Commit message can include hostname (caller responsibility)."""
        cfg = _cfg(tmp_path)
        repo = init_repo(cfg)

        (cfg.repo_path / "new.txt").write_text("data", encoding="utf-8")

        set_remote(repo, "https://github.com/user/dotfiles.git")
        with patch("git.remote.Remote.push"):
            commit_and_push(repo, "sync: myhost 2026-01-01")

        commits = list(repo.iter_commits())
        assert "myhost" in commits[0].message

    def test_pull_raises_on_conflict(self, tmp_path: Path) -> None:
        """MergeConflictError raised when unmerged blobs exist after pull."""
        cfg = _cfg(tmp_path)
        repo = init_repo(cfg)
        set_remote(repo, "https://github.com/user/dotfiles.git")

        repo.git = MagicMock()
        with patch("git.remote.Remote.fetch"), \
             patch.object(repo, "merge_base", return_value=["abc123"]), \
             patch(
                 "git.index.base.IndexFile.unmerged_blobs",
                 return_value={"file.txt": [(1, MagicMock())]},
             ):
            with pytest.raises(MergeConflictError, match="conflicts"):
                pull(repo)

    def test_push_raises_when_no_remote(self, tmp_path: Path) -> None:
        """NoRemoteConfiguredError raised when pushing without origin."""
        cfg = _cfg(tmp_path)
        repo = init_repo(cfg)

        (cfg.repo_path / "test.txt").write_text("hello", encoding="utf-8")

        with pytest.raises(NoRemoteConfiguredError):
            commit_and_push(repo, "test: push without remote")


# ---------------------------------------------------------------------------
# Step 4.7 — TestCopyToRepo
# ---------------------------------------------------------------------------


class TestCopyToRepo:
    def test_copy_to_repo_preserves_relative_path(self, tmp_path: Path) -> None:
        """File is placed at the correct relative path in the repo."""
        home = tmp_path / "home"
        repo_path = tmp_path / "repo"
        source = home / ".bashrc"
        home.mkdir()
        repo_path.mkdir()
        source.write_text("alias ls='ls --color'\n", encoding="utf-8")

        dest = copy_to_repo(source, home, repo_path)
        assert dest == repo_path / ".bashrc"
        assert dest.read_text(encoding="utf-8") == "alias ls='ls --color'\n"

    def test_copy_to_repo_creates_parent_dirs(self, tmp_path: Path) -> None:
        """Intermediate directories are created automatically."""
        home = tmp_path / "home"
        repo_path = tmp_path / "repo"
        source = home / ".config" / "nvim" / "init.lua"
        source.parent.mkdir(parents=True)
        repo_path.mkdir()
        source.write_text("-- nvim config\n", encoding="utf-8")

        dest = copy_to_repo(source, home, repo_path)
        assert dest == repo_path / ".config" / "nvim" / "init.lua"
        assert dest.exists()

    def test_copy_to_repo_overwrites_existing(self, tmp_path: Path) -> None:
        """Existing files in the repo are overwritten."""
        home = tmp_path / "home"
        repo_path = tmp_path / "repo"
        source = home / ".bashrc"
        home.mkdir()
        repo_path.mkdir()

        # Write old version
        old_dest = repo_path / ".bashrc"
        old_dest.write_text("old content\n", encoding="utf-8")

        # Write new source
        source.write_text("new content\n", encoding="utf-8")

        dest = copy_to_repo(source, home, repo_path)
        assert dest.read_text(encoding="utf-8") == "new content\n"
