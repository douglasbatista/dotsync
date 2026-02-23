"""Discovery and classification of configuration files."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel

from dotsync.config import CONFIG_DIR, DotSyncConfig
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
# Allowlists and excludes
# ---------------------------------------------------------------------------

KNOWN_FILES: frozenset[str] = frozenset(
    {
        ".bashrc",
        ".bash_profile",
        ".bash_aliases",
        ".profile",
        ".zshrc",
        ".zshenv",
        ".zprofile",
        ".zsh_history",
        ".gitconfig",
        ".gitignore_global",
        ".vimrc",
        ".tmux.conf",
        ".wgetrc",
        ".curlrc",
        ".inputrc",
        ".editorconfig",
        ".npmrc",
        ".yarnrc",
        ".pylintrc",
        ".flake8",
        ".prettierrc",
        ".eslintrc",
        ".hushlogin",
        ".config/starship.toml",
        ".config/fish/config.fish",
        ".config/nvim/init.vim",
        ".config/nvim/init.lua",
        ".config/alacritty/alacritty.toml",
        ".config/kitty/kitty.conf",
        ".config/hyper/.hyper.js",
        ".config/Code/User/settings.json",
        ".config/Code/User/keybindings.json",
        ".ssh/config",
    }
)

KNOWN_DIRS: frozenset[str] = frozenset(
    {
        ".config/nvim",
        ".config/fish",
        ".config/alacritty",
        ".config/kitty",
        ".config/Code/User/snippets",
    }
)

HARDCODED_EXCLUDES: frozenset[str] = frozenset(
    {
        # SSH private keys
        ".ssh/id_*",
        ".ssh/*.pem",
        # GPG keyrings
        ".gnupg/*",
        # Caches and histories
        ".cache/*",
        ".local/*",
        "__pycache__/*",
        "*.pyc",
        ".npm/_*",
        "node_modules/*",
        # OS artifacts
        ".DS_Store",
        "Thumbs.db",
        "desktop.ini",
        # Package lock files
        "package-lock.json",
        "yarn.lock",
        # Secrets and credentials
        "*.key",
        "*.pem",
        ".env",
        ".env.*",
        "credentials.json",
        "token.json",
        # Large / binary dirs
        ".vscode-server/*",
        ".cargo/*",
        ".rustup/*",
        ".nvm/*",
        ".pyenv/*",
    }
)

MAX_DEPTH = 4
MAX_FILE_SIZE = 1_000_000  # 1 MB
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


def _is_excluded(rel_str: str) -> bool:
    """Check if a relative path matches any hardcoded exclude pattern."""
    for pattern in HARDCODED_EXCLUDES:
        if fnmatch(rel_str, pattern):
            return True
        # Also check just the filename for patterns without directory
        if "/" not in pattern and fnmatch(Path(rel_str).name, pattern):
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

            # Skip symlink directories
            dirnames[:] = [
                d
                for d in dirnames
                if not (dirpath / d).is_symlink()
            ]

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

                # Apply hardcoded excludes
                if _is_excluded(rel_str):
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

    # Merge extra paths
    if extra_paths:
        for ep in extra_paths:
            ep_abs = ep if ep.is_absolute() else home / ep
            if ep_abs.is_file() and ep_abs.resolve() not in seen:
                seen.add(ep_abs.resolve())
                results.append(ep_abs)

    return results


# ---------------------------------------------------------------------------
# Step 2.3 — classify_rule_based
# ---------------------------------------------------------------------------


def _detect_os_profile(rel_str: str) -> Literal["linux", "windows", "shared"]:
    """Determine os_profile from the relative path."""
    if "AppData" in rel_str:
        return "windows"
    if rel_str.startswith(".config") or "/home/" in str(rel_str):
        return "linux"
    return "shared"


def classify_rule_based(
    candidates: list[Path],
    exclude_patterns: list[str] | None = None,
    include_extra: list[str] | None = None,
) -> list[ConfigFile]:
    """Classify candidates using rule-based matching.

    Args:
        candidates: Absolute paths from scan_candidates().
        exclude_patterns: User-configured glob patterns to exclude.
        include_extra: User-configured extra paths to include.

    Returns:
        List of ConfigFile with include/reason set where deterministic.
    """
    home = home_dir()
    exclude_patterns = exclude_patterns or []
    include_extra = include_extra or []

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
        reason = "unknown"

        # Check user exclude patterns first (highest priority)
        excluded_by_user = any(fnmatch(rel_str, pat) for pat in exclude_patterns)
        if excluded_by_user:
            include = False
            reason = "user_excluded"
        # Check user include_extra
        elif any(rel_str == p or str(fpath) == p for p in include_extra):
            include = True
            reason = "user_included"
        # Check known files (exact match on relative path)
        elif rel_str in KNOWN_FILES:
            include = True
            reason = "known"
        # Check known dirs (prefix match)
        elif any(rel_str.startswith(d + "/") or rel_str.startswith(d + os.sep) for d in KNOWN_DIRS):
            include = True
            reason = "known_dir"
        # Otherwise unknown / pending
        else:
            include = None
            reason = "unknown"

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
                "first_lines": _read_first_lines(cf.abs_path),
                "modified_days_ago": days_ago,
            }
        )

    prompt = (
        "You are a dotfile classifier. For each file, respond with a JSON array "
        "of objects with keys 'path' and 'verdict'. verdict must be one of: "
        "'include', 'exclude', or 'unknown'. Only output the JSON array, nothing else."
    )

    payload = {
        "model": cfg.llm_model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(items)},
        ],
        "temperature": 0,
    }

    try:
        resp = httpx.post(
            f"{cfg.llm_endpoint}/chat/completions",
            json=payload,
            timeout=15.0,
        )
        resp.raise_for_status()
        body = resp.json()
        content = body["choices"][0]["message"]["content"]
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

    except (httpx.HTTPError, KeyError, json.JSONDecodeError, TypeError, IndexError):
        for cf in to_classify:
            cf.include = None
            cf.reason = "ask_user"

    _save_classification_cache(cache)
    return candidates


# ---------------------------------------------------------------------------
# Step 2.5 — discover orchestrator
# ---------------------------------------------------------------------------


def discover(cfg: DotSyncConfig) -> list[ConfigFile]:
    """Discover and classify configuration files.

    Scans filesystem roots, applies rule-based classification, optionally
    queries AI for unknown files, and returns the full list.

    Args:
        cfg: DotSync configuration.

    Returns:
        List of ConfigFile with classification results.
    """
    extra = [Path(p) for p in cfg.include_extra] if cfg.include_extra else None
    candidates = scan_candidates(extra_paths=extra)

    classified = classify_rule_based(
        candidates,
        exclude_patterns=cfg.exclude_patterns,
        include_extra=[str(p) for p in (extra or [])],
    )

    # Separate unknowns for AI classification
    unknowns = [cf for cf in classified if cf.include is None]

    if unknowns and cfg.llm_endpoint:
        classify_with_ai(unknowns, cfg)

    # Any remaining None → ask_user
    for cf in classified:
        if cf.include is None:
            cf.reason = "ask_user"

    return classified
