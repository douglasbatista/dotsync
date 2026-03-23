"""Git repository and git-crypt integration for DotSync.

Wraps GitPython for standard Git operations and subprocess for git-crypt
(no Python bindings exist).  This module manages the dotfiles repository:
init, encryption, manifest tracking, commit/push/pull, and file copying.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

import git

from dotsync.config import DotSyncConfig
from dotsync.platform_utils import current_os

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MissingDependencyError(Exception):
    """Raised when git or git-crypt is not found on PATH."""


class GitCryptError(Exception):
    """Raised when a git-crypt subprocess call fails."""


class NoRemoteConfiguredError(Exception):
    """Raised when a push/pull is attempted without a configured remote."""


class MergeConflictError(Exception):
    """Raised when a pull results in merge conflicts."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITATTRIBUTES_CONTENT = """\
* filter=git-crypt diff=git-crypt
.gitattributes !filter !diff
.dotsync_manifest.json !filter !diff
"""

MANIFEST_FILENAME = ".dotsync_manifest.json"

# ---------------------------------------------------------------------------
# Step 4.1 — Dependency checks
# ---------------------------------------------------------------------------


def check_dependencies() -> None:
    """Verify that git and git-crypt are available on PATH.

    Raises:
        MissingDependencyError: If either tool is missing, with
            platform-appropriate install hints.
    """
    os_name = current_os()

    if shutil.which("git") is None:
        if os_name == "linux":
            hint = "Install with: sudo apt install git"
        else:
            hint = "Install from: https://git-scm.com/download/win"
        raise MissingDependencyError(f"git not found on PATH. {hint}")

    if shutil.which("git-crypt") is None:
        if os_name == "linux":
            hint = "Install with: sudo apt install git-crypt"
        else:
            hint = "Install from: https://github.com/AGWA/git-crypt"
        raise MissingDependencyError(f"git-crypt not found on PATH. {hint}")


# ---------------------------------------------------------------------------
# Step 4.2 — Repository initialization
# ---------------------------------------------------------------------------


def init_repo(cfg: DotSyncConfig) -> git.Repo:
    """Initialize (or open) the dotfiles Git repository.

    Creates the repo directory, writes ``.gitattributes`` (git-crypt config)
    and an empty ``.dotsync_manifest.json``, then makes an initial commit.
    Idempotent — returns the existing repo if ``.git`` already exists.

    Args:
        cfg: DotSync configuration with ``repo_path`` set.

    Returns:
        The initialized (or existing) ``git.Repo`` instance.
    """
    cfg.repo_path.mkdir(parents=True, exist_ok=True)

    if (cfg.repo_path / ".git").exists():
        return git.Repo(cfg.repo_path)

    repo = git.Repo.init(cfg.repo_path)

    # Write .gitattributes for git-crypt
    gitattributes = cfg.repo_path / ".gitattributes"
    gitattributes.write_text(GITATTRIBUTES_CONTENT, encoding="utf-8")

    # Write empty manifest
    manifest = cfg.repo_path / MANIFEST_FILENAME
    manifest.write_text("[]", encoding="utf-8")

    # Stage and commit
    repo.index.add([".gitattributes", MANIFEST_FILENAME])
    repo.index.commit("chore: init dotsync repo")

    return repo


# ---------------------------------------------------------------------------
# Step 4.3 — git-crypt init / unlock
# ---------------------------------------------------------------------------


def init_gitcrypt(repo_path: Path, key_export_path: Path) -> None:
    """Initialize git-crypt in a repository and export the symmetric key.

    Args:
        repo_path: Path to the Git repository.
        key_export_path: Destination path for the exported key file.

    Raises:
        GitCryptError: If the git-crypt commands fail.
    """
    try:
        subprocess.run(
            ["git-crypt", "init"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
        key_export_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git-crypt", "export-key", str(key_export_path)],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        raise GitCryptError(
            f"git-crypt init/export-key failed: {stderr}"
        ) from exc


def unlock_gitcrypt(repo_path: Path, key_path: Path) -> None:
    """Unlock a git-crypt repository with a symmetric key.

    Args:
        repo_path: Path to the Git repository.
        key_path: Path to the git-crypt symmetric key file.

    Raises:
        GitCryptError: If the unlock command fails.
    """
    try:
        subprocess.run(
            ["git-crypt", "unlock", str(key_path)],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        raise GitCryptError(f"git-crypt unlock failed: {stderr}") from exc


# ---------------------------------------------------------------------------
# Step 4.4 — Remote management
# ---------------------------------------------------------------------------


def set_remote(repo: git.Repo, remote_url: str) -> None:
    """Set or update the origin remote URL.

    If ``origin`` already exists, updates the URL; otherwise creates it.

    Args:
        repo: The Git repository.
        remote_url: The remote URL to set.
    """
    try:
        origin = repo.remotes.origin
        origin.set_url(remote_url)
    except (ValueError, AttributeError):
        repo.create_remote("origin", remote_url)


def get_remote(repo: git.Repo) -> str | None:
    """Return the origin remote URL, or None if not configured.

    Args:
        repo: The Git repository.

    Returns:
        The origin URL string, or ``None``.
    """
    try:
        return str(repo.remotes.origin.url)
    except (ValueError, AttributeError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Step 4.5 — Manifest management
# ---------------------------------------------------------------------------


@dataclass
class ManifestEntry:
    """A tracked file entry in the dotsync manifest."""

    relative_path: str
    os_profile: str
    added_at: str
    sensitive_flagged: bool


def load_manifest(repo_path: Path) -> list[ManifestEntry]:
    """Load the manifest from ``.dotsync_manifest.json``.

    Returns an empty list if the file is missing or corrupt.

    Args:
        repo_path: Path to the Git repository.

    Returns:
        List of manifest entries.
    """
    manifest_path = repo_path / MANIFEST_FILENAME
    if not manifest_path.exists():
        return []
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        return [ManifestEntry(**entry) for entry in data]
    except (json.JSONDecodeError, OSError, TypeError, KeyError):
        return []


def save_manifest(repo_path: Path, entries: list[ManifestEntry]) -> None:
    """Write manifest entries to ``.dotsync_manifest.json``.

    Args:
        repo_path: Path to the Git repository.
        entries: Manifest entries to write.
    """
    manifest_path = repo_path / MANIFEST_FILENAME
    data = [asdict(e) for e in entries]
    manifest_path.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8",
    )


def add_to_manifest(repo_path: Path, entry: ManifestEntry) -> None:
    """Add an entry to the manifest, deduplicating by relative_path.

    If an entry with the same ``relative_path`` already exists, it is
    not replaced.

    Args:
        repo_path: Path to the Git repository.
        entry: The entry to add.
    """
    entries = load_manifest(repo_path)
    existing_paths = {e.relative_path for e in entries}
    if entry.relative_path not in existing_paths:
        entries.append(entry)
        save_manifest(repo_path, entries)


def remove_from_manifest(repo_path: Path, relative_path: str) -> None:
    """Remove an entry from the manifest by relative_path.

    Args:
        repo_path: Path to the Git repository.
        relative_path: The relative path to remove.
    """
    entries = load_manifest(repo_path)
    filtered = [e for e in entries if e.relative_path != relative_path]
    save_manifest(repo_path, filtered)


# ---------------------------------------------------------------------------
# Step 4.6 — Commit, push, pull
# ---------------------------------------------------------------------------


def commit_and_push(repo: git.Repo, message: str) -> None:
    """Stage all changes, commit, and push to origin.

    Skips the commit if there are no staged changes and no untracked files.

    Args:
        repo: The Git repository.
        message: Commit message.

    Raises:
        NoRemoteConfiguredError: If no origin remote is configured.
    """
    repo.git.add(A=True)

    # Check if there's anything to commit
    if not repo.is_dirty(index=True) and not repo.untracked_files:
        return

    repo.index.commit(message)

    if get_remote(repo) is None:
        raise NoRemoteConfiguredError(
            "No remote configured. Set a remote with set_remote() before pushing."
        )

    branch = repo.active_branch.name
    push_infos = repo.remotes.origin.push(
        refspec=f"{branch}:{branch}", set_upstream=True
    )
    for info in push_infos:
        if info.flags & info.ERROR:
            raise git.GitCommandError(
                "git push",
                128,
                stderr=info.summary.strip() if info.summary else "push rejected",
            )


def pull(repo: git.Repo) -> None:
    """Pull from origin and check for merge conflicts.

    Args:
        repo: The Git repository.

    Raises:
        NoRemoteConfiguredError: If no origin remote is configured.
        MergeConflictError: If the pull results in merge conflicts.
    """
    if get_remote(repo) is None:
        raise NoRemoteConfiguredError(
            "No remote configured. Set a remote with set_remote() before pulling."
        )

    repo.remotes.origin.pull()

    if repo.index.unmerged_blobs():
        raise MergeConflictError(
            "Merge conflicts detected after pull. Resolve conflicts manually."
        )


# ---------------------------------------------------------------------------
# Step 4.7 — File copying
# ---------------------------------------------------------------------------


def copy_to_repo(source: Path, home: Path, repo_path: Path) -> Path:
    """Copy a file from the home directory into the repository.

    Preserves the relative path structure and file metadata.  Existing
    files at the destination are overwritten.

    Args:
        source: Absolute path to the source file.
        home: Home directory (used to compute relative path).
        repo_path: Path to the Git repository.

    Returns:
        The destination path within the repository.
    """
    rel = source.relative_to(home)
    dest = repo_path / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return dest
