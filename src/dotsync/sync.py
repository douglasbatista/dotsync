"""Sync engine for DotSync.

Orchestrates file operations between home directory and the dotfiles
repository: sync (home → repo), restore (repo → home), OS profile
filtering, cross-platform path transformation, dry-run mode, new file
registration, and conflict detection.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from dotsync.config import DotSyncConfig
from dotsync.discovery import ConfigFile
from dotsync.flagging import FlagResult
from dotsync.git_ops import ManifestEntry, add_to_manifest, copy_to_repo

# ---------------------------------------------------------------------------
# Step 5.1 — OS profile filter
# ---------------------------------------------------------------------------


def filter_by_profile(
    entries: list[ManifestEntry],
    current_os: Literal["linux", "windows"],
) -> list[ManifestEntry]:
    """Filter manifest entries by OS profile.

    Returns entries where ``os_profile`` matches *current_os* or is
    ``"shared"``.

    Args:
        entries: Manifest entries to filter.
        current_os: The current operating system.

    Returns:
        Filtered list of entries matching the current OS or shared.
    """
    return [
        e
        for e in entries
        if e.os_profile == current_os or e.os_profile == "shared"
    ]


# ---------------------------------------------------------------------------
# Step 5.2 — Path transformer
# ---------------------------------------------------------------------------

# Regex matching a source home path in value positions:
#   - after = or :  (config assignments)
#   - inside quotes
# Negative lookbehind avoids mangling URLs (https://, http://)
_VALUE_POSITION = r'(?:(?<=[=:])\s*|(?<=["\']))({home})'


def transform_paths(
    content: str,
    source_os: Literal["linux", "windows"],
    target_os: Literal["linux", "windows"],
    source_home: str,
    target_home: str,
) -> str:
    """Transform home-directory paths in file content across platforms.

    Replaces occurrences of *source_home* (in value positions) with
    *target_home*, flipping path separators within the replaced segment.

    No-op when *source_os* equals *target_os*.  URLs (``https://``,
    ``http://``) are never mangled.

    Args:
        content: File content to transform.
        source_os: OS the content was written on.
        target_os: OS the content is being restored to.
        source_home: Home directory path on the source OS.
        target_home: Home directory path on the target OS.

    Returns:
        Transformed content string.
    """
    if source_os == target_os:
        return content

    escaped_home = re.escape(source_home)

    # Match source_home after =, :, or opening quote, with optional
    # trailing path segments.  The preceding character is captured so
    # we can re-emit it (lookbehind can't handle the alternation here).
    pattern = re.compile(
        r"([=:\"'])\s*" + escaped_home + r"([/\\][^\s\"']*)?",
    )

    def _replace(m: re.Match[str]) -> str:
        prefix_char = m.group(1)
        full = m.group(0)

        # Skip URLs: if the prefix char is : check for http(s) before it
        start = m.start()
        before = content[max(0, start - 5) : start]
        if prefix_char == ":" and ("http" in before or "https" in before):
            return full

        # Everything after the prefix char
        after_prefix = full[1:]  # skip the captured [=:\"']

        # Replace source_home with target_home
        after_prefix = after_prefix.replace(source_home, target_home, 1)

        # Flip separators in the path portion only
        if source_os == "linux":
            # Linux → Windows: / → \  (only after the target_home)
            idx = after_prefix.find(target_home)
            if idx >= 0:
                before_home = after_prefix[:idx]
                home_and_rest = after_prefix[idx:]
                home_and_rest = home_and_rest.replace("/", "\\")
                after_prefix = before_home + home_and_rest
        else:
            # Windows → Linux: \ → /
            idx = after_prefix.find(target_home)
            if idx >= 0:
                before_home = after_prefix[:idx]
                home_and_rest = after_prefix[idx:]
                home_and_rest = home_and_rest.replace("\\", "/")
                after_prefix = before_home + home_and_rest

        return prefix_char + after_prefix

    return pattern.sub(_replace, content)


# ---------------------------------------------------------------------------
# Step 5.3 — Sync (home → repo)
# ---------------------------------------------------------------------------


@dataclass
class SyncAction:
    """A planned sync action (home → repo)."""

    source: Path
    destination: Path
    action: Literal["copy", "skip_missing", "skip_excluded"]
    transformed: bool


def plan_sync(
    entries: list[ManifestEntry],
    home: Path,
    repo_path: Path,
    current_os: Literal["linux", "windows"],
) -> list[SyncAction]:
    """Plan sync actions from home directory to repository.

    Filters entries by OS profile and checks which source files exist.

    Args:
        entries: Manifest entries to sync.
        home: Home directory path.
        repo_path: Path to the dotfiles repository.
        current_os: The current operating system.

    Returns:
        List of planned sync actions.
    """
    filtered = filter_by_profile(entries, current_os)
    actions: list[SyncAction] = []

    for entry in filtered:
        source = home / entry.relative_path
        destination = repo_path / entry.relative_path

        if not source.exists():
            actions.append(
                SyncAction(
                    source=source,
                    destination=destination,
                    action="skip_missing",
                    transformed=False,
                )
            )
        else:
            actions.append(
                SyncAction(
                    source=source,
                    destination=destination,
                    action="copy",
                    transformed=False,
                )
            )

    return actions


def execute_sync(
    actions: list[SyncAction],
    dry_run: bool = False,
) -> list[SyncAction]:
    """Execute planned sync actions.

    Copies files from home to repository.  In dry-run mode, no filesystem
    writes are performed.

    Args:
        actions: Planned sync actions from :func:`plan_sync`.
        dry_run: If ``True``, skip all filesystem writes.

    Returns:
        The full list of actions (for reporting).
    """
    if dry_run:
        return actions

    for act in actions:
        if act.action != "copy":
            continue
        act.destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(act.source, act.destination)

    return actions


# ---------------------------------------------------------------------------
# Step 5.4 — Restore (repo → home)
# ---------------------------------------------------------------------------


@dataclass
class RestoreAction:
    """A planned restore action (repo → home)."""

    source: Path
    destination: Path
    action: Literal["restore", "skip_missing_in_repo", "skip_profile"]
    transformed: bool


def plan_restore(
    entries: list[ManifestEntry],
    home: Path,
    repo_path: Path,
    current_os: Literal["linux", "windows"],
) -> list[RestoreAction]:
    """Plan restore actions from repository to home directory.

    Checks OS profile compatibility and repo file existence.

    Args:
        entries: Manifest entries to restore.
        home: Home directory path.
        repo_path: Path to the dotfiles repository.
        current_os: The current operating system.

    Returns:
        List of planned restore actions.
    """
    actions: list[RestoreAction] = []

    for entry in entries:
        # Check OS profile
        if entry.os_profile != current_os and entry.os_profile != "shared":
            actions.append(
                RestoreAction(
                    source=repo_path / entry.relative_path,
                    destination=home / entry.relative_path,
                    action="skip_profile",
                    transformed=False,
                )
            )
            continue

        repo_file = repo_path / entry.relative_path
        if not repo_file.exists():
            actions.append(
                RestoreAction(
                    source=repo_file,
                    destination=home / entry.relative_path,
                    action="skip_missing_in_repo",
                    transformed=False,
                )
            )
            continue

        actions.append(
            RestoreAction(
                source=repo_file,
                destination=home / entry.relative_path,
                action="restore",
                transformed=False,
            )
        )

    return actions


def execute_restore(
    actions: list[RestoreAction],
    dry_run: bool = False,
    source_os: Literal["linux", "windows"] | None = None,
    target_os: Literal["linux", "windows"] | None = None,
    source_home: str | None = None,
    target_home: str | None = None,
) -> list[RestoreAction]:
    """Execute planned restore actions.

    Copies files from repository to home directory.  Optionally applies
    cross-platform path transformation for shared files.  In dry-run
    mode, no filesystem writes are performed.

    Args:
        actions: Planned restore actions from :func:`plan_restore`.
        dry_run: If ``True``, skip all filesystem writes.
        source_os: OS the repo content was written on (for transforms).
        target_os: OS being restored to (for transforms).
        source_home: Home directory on the source OS (for transforms).
        target_home: Home directory on the target OS (for transforms).

    Returns:
        The full list of actions (for reporting).
    """
    if dry_run:
        return actions

    can_transform = all(
        v is not None
        for v in (source_os, target_os, source_home, target_home)
    )

    for act in actions:
        if act.action != "restore":
            continue

        act.destination.parent.mkdir(parents=True, exist_ok=True)

        if act.transformed and can_transform:
            assert source_os is not None
            assert target_os is not None
            assert source_home is not None
            assert target_home is not None
            content = act.source.read_text(encoding="utf-8", errors="replace")
            transformed = transform_paths(
                content, source_os, target_os, source_home, target_home
            )
            act.destination.write_text(transformed, encoding="utf-8")
        else:
            shutil.copy2(act.source, act.destination)

    return actions


# ---------------------------------------------------------------------------
# Step 5.5 — New file registration
# ---------------------------------------------------------------------------


def register_new_files(
    new_files: list[ConfigFile],
    flag_results: list[FlagResult],
    repo_path: Path,
    home: Path,
    cfg: DotSyncConfig,
    dry_run: bool = False,
) -> list[ManifestEntry]:
    """Register newly discovered files in the manifest and copy to repo.

    Only registers files that are in the confirmed set (not requiring
    confirmation) and marked ``include=True``.

    Args:
        new_files: Discovered config files to register.
        flag_results: Flagging results for sensitivity filtering.
        repo_path: Path to the dotfiles repository.
        home: Home directory path.
        cfg: DotSync configuration.
        dry_run: If ``True``, skip filesystem writes.

    Returns:
        List of newly created manifest entries.
    """
    # Build set of confirmed file paths (no confirmation required)
    confirmed: set[Path] = set()
    for fr in flag_results:
        if not fr.requires_confirmation:
            confirmed.add(fr.config_file.abs_path)

    now = datetime.now().astimezone().isoformat()
    new_entries: list[ManifestEntry] = []

    for cf in new_files:
        if cf.include is not True:
            continue
        if cf.abs_path not in confirmed:
            continue

        entry = ManifestEntry(
            relative_path=str(cf.path),
            os_profile=cf.os_profile,
            added_at=now,
            sensitive_flagged=cf.sensitive,
        )
        new_entries.append(entry)

        if not dry_run:
            copy_to_repo(cf.abs_path, home, repo_path)
            add_to_manifest(repo_path, entry)

    return new_entries


# ---------------------------------------------------------------------------
# Step 5.6 — Conflict detection
# ---------------------------------------------------------------------------


@dataclass
class Conflict:
    """A file with conflicting modifications in both home and repo."""

    relative_path: str
    local_mtime: datetime
    repo_mtime: datetime


def detect_conflicts(
    entries: list[ManifestEntry],
    home: Path,
    repo_path: Path,
    last_sync: datetime,
) -> list[Conflict]:
    """Detect files modified in both home and repo since last sync.

    A conflict is raised when both the local and repository copies have
    been modified after *last_sync*.  Missing files on either side are
    silently skipped (no conflict possible).

    Args:
        entries: Manifest entries to check.
        home: Home directory path.
        repo_path: Path to the dotfiles repository.
        last_sync: Timestamp of the last successful sync.

    Returns:
        List of detected conflicts.
    """
    conflicts: list[Conflict] = []

    for entry in entries:
        local_path = home / entry.relative_path
        repo_file = repo_path / entry.relative_path

        if not local_path.exists() or not repo_file.exists():
            continue

        local_mtime = datetime.fromtimestamp(
            local_path.stat().st_mtime,
        ).astimezone()
        repo_mtime = datetime.fromtimestamp(
            repo_file.stat().st_mtime,
        ).astimezone()

        if local_mtime > last_sync and repo_mtime > last_sync:
            conflicts.append(
                Conflict(
                    relative_path=entry.relative_path,
                    local_mtime=local_mtime,
                    repo_mtime=repo_mtime,
                )
            )

    return conflicts
