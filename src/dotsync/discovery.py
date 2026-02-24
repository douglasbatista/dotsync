"""Discovery and classification of configuration files."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from dotsync.config import CONFIG_DIR, DotSyncConfig
from dotsync.llm_client import LLMError, chat_completion
from dotsync.platform_utils import config_dirs, home_dir

# ---------------------------------------------------------------------------
# ConfigFile model
# ---------------------------------------------------------------------------


class ConfigFile(BaseModel):
    """A discovered configuration file with classification metadata."""

    path: Path
    """Relative path of the config file (relative to home)."""

    abs_path: Path
    """Absolute path on disk."""

    size_bytes: int
    """File size in bytes."""

    include: bool | None = None
    """True = include, False = exclude, None = pending/unknown."""

    reason: str = "unknown"
    """Classification reason, e.g. 'known', 'user_excluded', 'ai:include'."""

    os_profile: Literal["linux", "windows", "shared"] = "shared"
    """Which OS profile this file belongs to."""


# ---------------------------------------------------------------------------
# Exclude lists and heuristic rules
# ---------------------------------------------------------------------------

SAFETY_EXCLUDES: list[str] = [
    ".ssh/id_*",
    ".ssh/id_*.pub",
    ".gnupg/",
    ".dotsync/",
    "dotsync.key",
]

SCAN_EXCLUDES: list[str] = [
    ".cache/",
    ".local/share/",
    ".local/lib/",
    "node_modules/",
    "__pycache__/",
    ".git/",
    ".venv/",
    "venv/",
    ".tox/",
]

HEURISTIC_RULES: list[dict] = [
    {"pattern": "is_home_dotfile", "max_depth": 1, "reason": "home dotfile"},
    {"pattern": "under_config_dir", "max_depth": 3, "reason": "XDG config"},
    {
        "pattern": "windows_appdata",
        "max_depth": 4,
        "reason": "Windows app config",
        "extensions": [".json", ".toml", ".yaml", ".yml", ".ini", ".conf", ".xml", ".cfg"],
    },
    {
        "pattern": "config_extension",
        "max_depth": 2,
        "reason": "config extension",
        "extensions": [".toml", ".yaml", ".yml", ".ini", ".conf", ".cfg"],
    },
]

MAX_DEPTH = 5
MAX_FILE_SIZE = 512_000
BINARY_CHECK_BYTES = 8192

CLASSIFICATION_CACHE_FILE = CONFIG_DIR / "classification_cache.json"


# ---------------------------------------------------------------------------
# Step 2.2 — scan_candidates
# ---------------------------------------------------------------------------


def _is_binary(path: Path) -> bool:
    """Check if a file appears to be binary by looking for null bytes."""
    try:
        with path.open("rb") as f:
            chunk = f.read(BINARY_CHECK_BYTES)
        return b"\x00" in chunk
    except OSError:
        return True


def _is_excluded(rel_str: str, patterns: list[str]) -> bool:
    """Check if a relative path matches any exclude pattern.

    Supports glob patterns (e.g. ``.ssh/id_*``) via fnmatch and
    directory prefixes with trailing slash (e.g. ``.cache/``).
    """
    for pattern in patterns:
        if pattern.endswith("/"):
            # Directory-style pattern: match as prefix
            if rel_str.startswith(pattern) or rel_str == pattern.rstrip("/"):
                return True
        elif fnmatch(rel_str, pattern):
            return True
    return False


def scan_candidates(extra_paths: list[Path] | None = None) -> list[Path]:
    """Scan config directories for candidate config files.

    Walks each root from config_dirs() up to MAX_DEPTH, skipping
    symlinks, large files, binary files, and hardcoded excludes.

    Args:
        extra_paths: Additional absolute paths to include.

    Returns:
        Deduplicated list of absolute Paths.
    """
    home = home_dir()
    seen: set[Path] = set()
    results: list[Path] = []

    roots = config_dirs()

    for root in roots:
        if not root.is_dir():
            continue

        root_depth = str(root).count(os.sep)

        for dirpath_str, dirnames, filenames in os.walk(root, followlinks=False):
            dirpath = Path(dirpath_str)

            # Depth check relative to root
            current_depth = str(dirpath).count(os.sep) - root_depth
            if current_depth >= MAX_DEPTH:
                dirnames.clear()
                continue

            # Skip symlink directories and scan-excluded directories
            filtered: list[str] = []
            for d in dirnames:
                child = dirpath / d
                if child.is_symlink():
                    continue
                try:
                    rel_d = str(child.relative_to(home)) + "/"
                except ValueError:
                    rel_d = d + "/"
                if _is_excluded(rel_d, SCAN_EXCLUDES):
                    continue
                filtered.append(d)
            dirnames[:] = filtered

            for fname in filenames:
                fpath = dirpath / fname

                # Skip symlinks
                if fpath.is_symlink():
                    continue

                resolved = fpath.resolve()
                if resolved in seen:
                    continue

                # Relative path for filtering
                try:
                    rel = fpath.relative_to(home)
                except ValueError:
                    rel = fpath
                rel_str = str(rel)

                # Apply safety excludes
                if _is_excluded(rel_str, SAFETY_EXCLUDES):
                    continue

                # Skip large files
                try:
                    size = fpath.stat().st_size
                except OSError:
                    continue
                if size > MAX_FILE_SIZE:
                    continue

                # Skip binary files
                if size > 0 and _is_binary(fpath):
                    continue

                seen.add(resolved)
                results.append(fpath)

    # Merge extra paths (still subject to safety excludes)
    if extra_paths:
        for ep in extra_paths:
            ep_abs = ep if ep.is_absolute() else home / ep
            if not ep_abs.is_file():
                continue
            try:
                ep_rel = str(ep_abs.relative_to(home))
            except ValueError:
                ep_rel = str(ep_abs)
            if _is_excluded(ep_rel, SAFETY_EXCLUDES):
                continue
            if ep_abs.resolve() not in seen:
                seen.add(ep_abs.resolve())
                results.append(ep_abs)

    return results


# ---------------------------------------------------------------------------
# Step 2.3 — heuristic classifier
# ---------------------------------------------------------------------------


def _detect_os_profile(rel_str: str) -> Literal["linux", "windows", "shared"]:
    """Determine os_profile from the relative path."""
    if "AppData" in rel_str:
        return "windows"
    if rel_str.startswith(".config") or "/home/" in str(rel_str):
        return "linux"
    return "shared"


def _matches_heuristic(rel: Path, rule: dict) -> bool:
    """Check if a relative path matches a heuristic rule.

    Depth is counted as number of path parts after the anchor directory.
    """
    parts = rel.parts
    n_parts = len(parts)
    pattern = rule["pattern"]
    max_depth = rule["max_depth"]
    extensions = rule.get("extensions", [])

    if pattern == "is_home_dotfile":
        return n_parts == 1 and parts[0].startswith(".")

    if pattern == "under_config_dir":
        return parts[0] == ".config" and (n_parts - 1) <= max_depth

    if pattern == "windows_appdata":
        try:
            appdata_idx = list(parts).index("AppData")
        except ValueError:
            return False
        if (n_parts - appdata_idx - 1) > max_depth:
            return False
        return rel.suffix in extensions

    if pattern == "config_extension":
        return (n_parts - 1) <= max_depth and rel.suffix in extensions

    return False


def classify_heuristic(
    candidates: list[Path],
    cfg: DotSyncConfig,
) -> list[ConfigFile]:
    """Classify candidates using structural heuristic rules.

    Args:
        candidates: Absolute paths from scan_candidates().
        cfg: DotSync configuration.

    Returns:
        List of ConfigFile with include/reason set where deterministic.
    """
    home = home_dir()
    exclude_patterns = cfg.exclude_patterns or []
    include_extra = cfg.include_extra or []

    results: list[ConfigFile] = []

    for fpath in candidates:
        try:
            rel = fpath.relative_to(home)
        except ValueError:
            rel = fpath
        rel_str = str(rel)

        try:
            size = fpath.stat().st_size
        except OSError:
            continue

        os_profile = _detect_os_profile(rel_str)
        include: bool | None = None
        reason = "ambiguous"

        # Check user exclude patterns first (highest priority)
        excluded_by_user = any(fnmatch(rel_str, pat) for pat in exclude_patterns)
        if excluded_by_user:
            include = False
            reason = "user_excluded"
        # Check user include_extra
        elif any(rel_str == p or str(fpath) == p for p in include_extra):
            include = True
            reason = "user_included"
        # Check heuristic rules (first match wins)
        else:
            for rule in HEURISTIC_RULES:
                if _matches_heuristic(rel, rule):
                    include = True
                    reason = rule["reason"]
                    break

        results.append(
            ConfigFile(
                path=rel,
                abs_path=fpath,
                size_bytes=size,
                include=include,
                reason=reason,
                os_profile=os_profile,
            )
        )

    return results


# ---------------------------------------------------------------------------
# Step 2.4 — classify_with_ai
# ---------------------------------------------------------------------------


def _load_classification_cache() -> dict[str, dict]:
    """Load cached AI classification results."""
    if not CLASSIFICATION_CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(CLASSIFICATION_CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_classification_cache(cache: dict[str, dict]) -> None:
    """Persist AI classification cache to disk."""
    CLASSIFICATION_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CLASSIFICATION_CACHE_FILE.write_text(
        json.dumps(cache, indent=2),
        encoding="utf-8",
    )


def _read_first_lines(path: Path, n: int = 5) -> list[str]:
    """Read first n lines of a file, returning empty list on error."""
    try:
        lines: list[str] = []
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for _ in range(n):
                line = f.readline()
                if not line:
                    break
                lines.append(line.rstrip("\n"))
        return lines
    except OSError:
        return []


def classify_with_ai(
    candidates: list[ConfigFile],
    cfg: DotSyncConfig,
) -> list[ConfigFile]:
    """Classify unknown candidates using an LLM via LiteLLM proxy.

    Args:
        candidates: ConfigFile objects with include=None to classify.
        cfg: DotSync configuration (needs llm_endpoint set).

    Returns:
        Updated list of ConfigFile with AI verdicts applied.
    """
    if not cfg.llm_endpoint:
        return candidates

    cache = _load_classification_cache()

    # Separate cached from uncached
    to_classify: list[ConfigFile] = []
    for cf in candidates:
        key = str(cf.path)
        if key in cache:
            cached = cache[key]
            cf.include = cached.get("include")
            cf.reason = cached.get("reason", "ai:cached")
        else:
            to_classify.append(cf)

    if not to_classify:
        return candidates

    # Build payload
    now = datetime.now(tz=timezone.utc)
    items: list[dict] = []
    for cf in to_classify:
        try:
            mtime = cf.abs_path.stat().st_mtime
            days_ago = (now - datetime.fromtimestamp(mtime, tz=timezone.utc)).days
        except OSError:
            days_ago = -1

        items.append(
            {
                "path": str(cf.path),
                "size_bytes": cf.size_bytes,
                "first_lines": "\n".join(_read_first_lines(cf.abs_path)),
                "modified_days_ago": days_ago,
            }
        )

    system_prompt = (
        "You are a dotfile classifier. For each file, respond with a JSON array "
        "of objects with keys 'path', 'verdict', and 'reason'. verdict must be "
        "one of: 'include', 'exclude', or 'ask_user'. Only output the JSON array, "
        "nothing else."
    )

    try:
        content = chat_completion(
            endpoint=cfg.llm_endpoint,
            model=cfg.llm_model,
            system_prompt=system_prompt,
            user_message=json.dumps(items),
        )
        verdicts_raw = json.loads(content)

        verdict_map: dict[str, str] = {}
        if isinstance(verdicts_raw, list):
            for v in verdicts_raw:
                if isinstance(v, dict) and "path" in v and "verdict" in v:
                    verdict_map[v["path"]] = v["verdict"]

        for cf in to_classify:
            key = str(cf.path)
            verdict = verdict_map.get(key)
            if verdict == "include":
                cf.include = True
                cf.reason = "ai:include"
            elif verdict == "exclude":
                cf.include = False
                cf.reason = "ai:exclude"
            else:
                cf.include = None
                cf.reason = "ask_user"

            cache[key] = {"include": cf.include, "reason": cf.reason}

    except (LLMError, json.JSONDecodeError, TypeError):
        for cf in to_classify:
            cf.include = None
            cf.reason = "ask_user"

    _save_classification_cache(cache)
    return candidates


# ---------------------------------------------------------------------------
# Step 2.6 — discover orchestrator
# ---------------------------------------------------------------------------


def discover(cfg: DotSyncConfig) -> list[ConfigFile]:
    """Discover and classify configuration files.

    Scans filesystem roots, applies heuristic classification, optionally
    queries AI for ambiguous files, and returns the full list.

    Args:
        cfg: DotSync configuration.

    Returns:
        List of ConfigFile with classification results.
    """
    extra = [Path(p) for p in cfg.include_extra] if cfg.include_extra else None
    candidates = scan_candidates(extra_paths=extra)

    classified = classify_heuristic(candidates, cfg)

    # Separate ambiguous for AI classification
    ambiguous = [cf for cf in classified if cf.include is None]

    if ambiguous and cfg.llm_endpoint:
        classify_with_ai(ambiguous, cfg)

    # Any remaining None → ask_user
    for cf in classified:
        if cf.include is None:
            cf.reason = "ask_user"

    return classified
