"""Discovery and classification of configuration files."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable, Literal, TypedDict

logger = logging.getLogger("dotsync")

from pydantic import BaseModel

from dotsync.config import CONFIG_DIR, DotSyncConfig
from dotsync.llm_client import LLMError, chat_completion
from dotsync.platform_utils import config_dirs, home_dir


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------


class ScanEvent(TypedDict):
    """Event emitted during scan for progress reporting."""

    type: Literal[
        "root_start",
        "root_done",
        "dir_enter",
        "dir_pruned",
        "file_accepted",
        "file_rejected",
        "phase_start",
        "phase_done",
        "ai_batch",
        "ai_error",
    ]
    path: str | None
    reason: str | None
    count: int | None


ProgressCallback = Callable[[ScanEvent], None]


def _emit(progress: ProgressCallback | None, event: ScanEvent) -> None:
    """Safely emit a progress event if a callback is provided."""
    if progress is not None:
        progress(event)

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

    sensitive: bool = False
    """Whether the file was flagged as containing sensitive data."""

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

PRUNE_DIRS: list[str] = [
    # Version control
    ".git", ".hg", ".svn",
    # Dependency trees
    "node_modules", ".pnpm-store",
    # Python
    "__pycache__", ".venv", "venv", ".tox", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    # Rust / Cargo
    "registry",
    # Build output
    "dist", "build", "target", "out", ".next", ".nuxt",
    # Runtime / generated state
    ".cache", "Cache", "cache",
    "logs", "log", "tmp", "temp", "Temp",
    # Electron / Chromium caches
    "GPUCache", "ShaderCache", "DawnCache", "Code Cache",
    "CachedData", "CachedExtensions", "CachedConfigurations",
    "blob_storage", "CacheStorage", "htmlcache",
    # IDE state
    "caches", "snapshots", "index", "History",
    # VS Code server — binaries, bundled extensions, not user config
    "bin", "extensions",
    # App internal state directories
    "file-history", "backups", "todos",
    # Installed program files (not user config)
    "Programs",
    # Internet Explorer DOM storage
    "DOMStore",
    # Shell plugin code and themes (oh-my-zsh, zinit, etc.)
    "plugins", "themes", "custom",
    # Locale / i18n bundles
    "l10n", "locales", "locale",
    # License / legal files directory
    "licenses",
    # AI agent state directories
    "projects", "tasks", "conversations", "events", "subagents",
    # Shell prompt / theme engine internals
    "language", "gitstatus",
    # GitHub / GitLab repository metadata (project config, not user config)
    ".github", ".gitlab",
]
_PRUNE_DIRS_SET: frozenset[str] = frozenset(PRUNE_DIRS)

_PRUNE_PREFIXES: list[str] = [
    ".local/share/",
    ".local/lib/",
]

ALLOWED_EXTENSIONS: frozenset[str] = frozenset({
    ".toml", ".yaml", ".yml", ".json", ".jsonc", ".ini",
    ".cfg", ".conf", ".config", ".xml", ".properties",
    ".env", ".rc", ".plist",
})

ALLOWED_NAMED_FILES: frozenset[str] = frozenset({
    "config", "credentials",
})

HOME_BLOCKED_DOTFILES: frozenset[str] = frozenset({
    ".bash_history", ".zsh_history", ".zsh_sessions",
    ".python_history", ".node_repl_history",
    ".dbshell", ".mysql_history", ".psql_history",
    ".viminfo", ".viminfo.tmp",
    ".lesshst", ".wget-hsts",
    ".ICEauthority", ".Xauthority",
    ".sudo_as_admin_successful", ".motd_shown",
    ".xsession-errors", ".xsession-errors.old",
    ".bash_logout",
})

# Compiled once at module load.
# Applied to both stem (filename without last extension) and full name.
BLOCKED_FILENAME_PATTERNS: list[re.Pattern[str]] = [
    # UUID / GUID with optional leading dot (e.g. .f9c91a88-3095-44a3-bbb5-011673bd7cc9)
    re.compile(
        r"^\.?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        re.IGNORECASE,
    ),
    # Pure hex 8+ chars, optionally followed by @version (short hashes, git SHAs, cache keys)
    re.compile(r"^[0-9a-f]{8,}(@[a-z0-9]+)?$", re.IGNORECASE),
    # Pure numeric filenames (generated IDs, timestamps as filenames)
    re.compile(r"^\d+$"),
    # Hex with dots/dashes only — e.g. "a1b2c3d4.e5f6" (VS Code extension storage)
    re.compile(r"^[0-9a-f]{6,}[.\-][0-9a-f]{4,}$", re.IGNORECASE),
    # Trailing Unix timestamp (10+ digits) after a dot — e.g. .claude.json.backup.1772283029203
    re.compile(r"\.\d{10,}$"),
    # Version-number directories/files — e.g. "4.10.2934.0", "120.0.6050.0"
    re.compile(r"^\d+(\.\d+){2,}$"),
    # Embedded hex 32+ chars (SHA-256, etc.) — e.g. "config.133e14b5...e4e1.omp"
    re.compile(r"[0-9a-f]{32,}", re.IGNORECASE),
]


def _is_generated_filename(path: Path) -> bool:
    """Check if a filename looks auto-generated (UUID, hex hash, numeric ID)."""
    stem = path.stem
    name = path.name
    return any(p.search(stem) or p.search(name) for p in BLOCKED_FILENAME_PATTERNS)


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
MAX_FILE_SIZE = 50_000
BINARY_CHECK_BYTES = 512
MAX_FIRST_LINES = 5
MAX_FIRST_LINES_CHARS = 200
MAX_CANDIDATES_PER_BATCH = 10

CLASSIFICATION_CACHE_FILE = CONFIG_DIR / "classification_cache.json"

SYSTEM_PROMPT = """\
You are classifying files found in a user's home directory for a dotfile sync tool.

The question is NOT "did the user write this?" or "is this a config file?".

The question is:
"Is this file part of the user's computing environment that should be consistent across
machines — or is it internal infrastructure that the tool manages and would recreate
automatically on reinstall?"

A file belongs in the user's environment if losing it would change how any tool behaves
for this user, even if they never edited it. Default configs count. Pinned versions count.
Unmodified configs that the user wants identical on every machine count.

A file is internal infrastructure if the tool that owns it would regenerate it
automatically on reinstall with equivalent content, and it carries no
user-specific or machine-specific meaning.

Classify each file as one of:
- "include"   — part of the user's environment; losing it changes how something behaves
- "exclude"   — internal tooling infrastructure; the tool recreates it automatically
- "ask_user"  — genuinely cannot determine from path and content alone

EXCLUDE confidently if ANY of the following are true:
- The file is a cache, index, lock, marker, or accounting file the tool writes to track its own state
- The file is source code, a library, or a template that ships with the tool installation
- The file is build/packaging scaffolding: package.json, tsconfig.json, Makefile, setup.py,
  pyproject.toml, Cargo.toml, etc. inside a tool's config directory — these define the tool's own
  build process, not user preferences. They are recreated when the tool is reinstalled
- The file belongs to a project repository (CI config, funding declarations, contributor files)
- The content is generated or machine-written with no user-specific values
- Reinstalling the tool would produce this file with identical content
- The path contains segments suggesting temporary or historical data (e.g. "tmp", "temp", "history",
  "backup", "old", "archive", "trash", "undo", "recovery", "session", "swap")
- The path or filename contains hex hashes (32+ hex chars), UUIDs, or purely numeric indexes — these
  indicate auto-generated or cache-keyed storage, not user configuration. For example a filename like
  "config.133e14b50b2ae0ab.omp.json" is a cached copy keyed by content hash, not a user config file
- The file is a server-pushed feature flag, A/B test config, or remote-fetched policy file that the
  app downloads on startup and would re-download on reinstall (e.g. NordVPN FeatureConfigs,
  Docker unleash toggle files, app onboarding configs)
- The file is an addon/plugin/extension manifest OR default settings (addon.xml, manifest.json,
  resources/settings.xml) that ships with the addon installation — these are recreated when the
  addon is reinstalled. Files under "addons/*/resources/" are addon defaults, not user overrides;
  user-customized addon settings live in "userdata/addon_data/", not inside the addon directory
- The file tracks internal app state: window positions, notification read-state, install timestamps,
  update-check results, feedback history, periodic task schedules, version metadata, or project/file
  history (recently opened projects, recent files, workspace lists) — these are machine-specific
  and not portable
- The file is IDE/editor internal storage (globalStorage/storage.json, argv.json, workspace state)
  — these contain machine-specific telemetry IDs, window state, and paths, not user preferences.
  User settings in editors live in files explicitly named "settings.json" or "keybindings.json"
  under a User or profile directory
- The file belongs to OEM/vendor bloatware (e.g. Lenovo Vantage, Dell SupportAssist) — these are
  machine-specific manufacturer tools whose config is not portable across machines
- The file is a VPN/security app's auto-generated settings dump with server lists, routing tables,
  or connection profiles fetched from the provider — these are large, machine-specific, and
  regenerated on login
- The file is a server/cluster list, CDN endpoint map, or infrastructure topology that the app
  fetches from its backend — the content is identical for all users
- The file is desktop.ini (Windows auto-generated folder metadata) or similar OS-generated markers
- The file contains login tokens, session cookies, or auth credentials (login-info, auth-token, etc.)
  — these are machine-specific secrets, not portable config
- The file is telemetry/reporting data, data-collection consent flags, or analytics state
- The file is an installation receipt, installer state, or upgrade-pending marker
- The file is a browser's built-in search engine list, partner speed dials, keyword index, or
  assistant preferences — these ship with the browser and are not user customizations

INCLUDE confidently if ANY of the following are true:
- The file controls how a tool behaves for this user, even if never manually edited
- The file reflects a choice — installed plugins, selected theme, registry mirror, auth context
- Losing or changing this file would produce a different experience on a fresh machine
- The file is named settings.json, config.yaml, *.conf, *.ini, or similar AND contains values the
  user chose or customized (keybindings, theme selection, editor preferences, shell prompt config)
- BUT: a file named "settings.json" or "config.json" is NOT automatically included — check the
  content and path. If it contains machine IDs, telemetry state, window geometry, or is under an
  internal storage directory, it is app state, not user config

Use "ask_user" sparingly. Only when path and content together give no clear signal.
Credentials and private keys are outside your scope — they are handled separately.

Input: a JSON array of candidate files, each with:
  - "path": relative to home directory
  - "size_bytes": file size
  - "first_lines": up to 5 lines / 200 chars of content (may be truncated with "...")
  - "modified_days_ago": days since last modification

Output: a JSON array — one entry per input file, same order, no extra keys, no markdown fences:
[
  {"path": "<same path from input>", "verdict": "include"|"exclude"|"ask_user", "reason": "<one short sentence>"}
]

Respond with ONLY the JSON array. No explanation, no markdown, no preamble.\
"""


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


def _should_prune_dir(
    entry: os.DirEntry[str],
    home: Path,
    repo_path: Path | None = None,
) -> str | None:
    """Check if a directory should be pruned by name, prefix, safety, or repo path.

    Returns None if the directory should not be pruned, or a reason string.
    """
    name = entry.name
    if name in _PRUNE_DIRS_SET:
        return "dir_name in PRUNE_DIRS"
    try:
        rel = str(Path(entry.path).relative_to(home))
    except ValueError:
        rel = name
    rel_slash = rel + "/"
    for prefix in _PRUNE_PREFIXES:
        if rel_slash.startswith(prefix) or rel == prefix.rstrip("/"):
            return f"prefix match: {prefix}"
    if _is_excluded(rel_slash, SAFETY_EXCLUDES):
        return "safety_exclude"
    if repo_path is not None and Path(entry.path).resolve() == repo_path.resolve():
        return "repo_path"
    if _is_generated_filename(Path(name)):
        return "generated dir name"
    return None


def _prefilter_file(
    path: Path,
    stat: os.stat_result,
    home: Path,
    is_home_root: bool = False,
) -> str | None:
    """Check if a file passes all pre-filter checks.

    Uses a whitelist approach: only files with known config extensions
    (or recognised extensionless names) are accepted.

    Checks are ordered from cheapest to most expensive:
    safety excludes, size, whitelist gate, binary.

    Args:
        path: Absolute path to the file.
        stat: Pre-populated stat result from scandir.
        home: User home directory.
        is_home_root: True when scanning direct children of ``$HOME``.

    Returns None if the file passes, or a rejection reason string.
    """
    try:
        rel = path.relative_to(home)
    except ValueError:
        rel = path
    rel_str = str(rel)

    # Safety excludes — non-overridable
    if _is_excluded(rel_str, SAFETY_EXCLUDES):
        return "safety_exclude"

    # Size — free, stat already populated by scandir
    if stat.st_size > MAX_FILE_SIZE:
        return f"size: {stat.st_size} > {MAX_FILE_SIZE}"

    # Whitelist gate
    name = path.name
    suffix = path.suffix.lower()
    # Python treats ".env" as stem with no suffix; check the name itself as extension
    effective_ext = suffix if suffix else (name.lower() if name.startswith(".") else "")

    if is_home_root and name.startswith("."):
        # Direct $HOME dotfiles: reject known noise, accept extensionless or allowed ext
        if name in HOME_BLOCKED_DOTFILES:
            return f"home blocked dotfile: {name}"
        if not suffix or suffix in ALLOWED_EXTENSIONS or effective_ext in ALLOWED_EXTENSIONS:
            pass  # accepted
        else:
            return f"not in whitelist: {name}"
    elif suffix in ALLOWED_EXTENSIONS:
        pass  # accepted
    elif effective_ext in ALLOWED_EXTENSIONS:
        pass  # accepted — e.g. .env (Python sees no suffix)
    elif not suffix and name in ALLOWED_NAMED_FILES:
        pass  # accepted — e.g. .ssh/config, .aws/credentials
    else:
        return f"not in whitelist: {name}"

    # Generated filename — hash, UUID, numeric, version-numbered
    if _is_generated_filename(path):
        return f"generated filename: {name}"

    # Binary — only check that touches file content
    if stat.st_size > 0 and _is_binary(path):
        return "binary file"

    return None


def _scan_dir(
    root: Path,
    depth: int,
    max_depth: int,
    home: Path,
    repo_path: Path | None = None,
    progress: ProgressCallback | None = None,
) -> list[Path]:
    """Recursively scan a directory using os.scandir().

    Uses DirEntry objects for efficient stat access. Prunes entire
    subtrees at directory entry level before recursion.
    """
    _emit(progress, {"type": "dir_enter", "path": str(root), "reason": None, "count": None})
    candidates: list[Path] = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    prune_reason = _should_prune_dir(entry, home, repo_path)
                    if prune_reason is not None:
                        _emit(progress, {
                            "type": "dir_pruned",
                            "path": entry.path,
                            "reason": prune_reason,
                            "count": None,
                        })
                        continue
                    if depth < max_depth:
                        candidates.extend(
                            _scan_dir(
                                Path(entry.path), depth + 1, max_depth,
                                home, repo_path, progress,
                            )
                        )
                elif entry.is_file(follow_symlinks=False):
                    path = Path(entry.path)
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    is_home_root = root == home and depth == 0
                    reject_reason = _prefilter_file(path, stat, home, is_home_root)
                    if reject_reason is not None:
                        _emit(progress, {
                            "type": "file_rejected",
                            "path": entry.path,
                            "reason": reject_reason,
                            "count": None,
                        })
                    else:
                        _emit(progress, {
                            "type": "file_accepted",
                            "path": entry.path,
                            "reason": None,
                            "count": None,
                        })
                        candidates.append(path)
    except PermissionError:
        pass  # silently skip inaccessible dirs
    return candidates


def scan_candidates(
    extra_paths: list[Path] | None = None,
    repo_path: Path | None = None,
    progress: ProgressCallback | None = None,
) -> list[Path]:
    """Scan config directories for candidate config files.

    Uses os.scandir() with manual recursion for efficient scanning.
    Scan roots are walked in parallel using ThreadPoolExecutor.
    Each root has its own max_depth as returned by :func:`config_dirs`.

    Args:
        extra_paths: Additional absolute paths to include.
            Bypass pruning and blocked lists but NOT safety excludes.
        repo_path: Path to the dotfiles repository — always excluded
            from scanning regardless of name or location.
        progress: Optional callback for real-time scan progress events.

    Returns:
        Deduplicated list of absolute Paths.
    """
    home = home_dir()
    roots = [(r, d) for r, d in config_dirs() if r.is_dir()]

    # Parallel scan across roots
    all_candidates: list[Path] = []
    if roots:
        with ThreadPoolExecutor(max_workers=len(roots)) as executor:
            futures = {}
            for root, max_depth in roots:
                _emit(progress, {
                    "type": "root_start",
                    "path": str(root),
                    "reason": None,
                    "count": None,
                })
                futures[executor.submit(
                    _scan_dir, root, 0, max_depth, home, repo_path, progress,
                )] = root
            for future in as_completed(futures):
                root = futures[future]
                try:
                    result = future.result()
                except Exception:
                    logger.debug("scan failed for root %s", root, exc_info=True)
                    result = []
                all_candidates.extend(result)
                _emit(progress, {
                    "type": "root_done",
                    "path": str(root),
                    "reason": None,
                    "count": len(result),
                })

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
            all_candidates.append(ep_abs)

    # Deduplicate, preserve order
    seen: set[Path] = set()
    results: list[Path] = []
    for p in all_candidates:
        resolved = p.resolve()
        if resolved not in seen:
            seen.add(resolved)
            results.append(p)

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
        elif any(rel_str == str(p) or str(fpath) == str(p) for p in include_extra):
            include = True
            reason = "user_included"
        # Check heuristic rules — tag reason but leave include=None for AI
        else:
            for rule in HEURISTIC_RULES:
                if _matches_heuristic(rel, rule):
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


def _read_first_lines(
    path: Path,
    n: int = MAX_FIRST_LINES,
    max_chars: int = MAX_FIRST_LINES_CHARS,
) -> str:
    """Read first *n* lines of a file, capped at *max_chars* total characters.

    Returns the joined text (lines separated by ``\\n``).  If the raw text
    exceeds *max_chars*, it is truncated and ``"..."`` is appended.
    Returns an empty string on read error.
    """
    try:
        lines: list[str] = []
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for _ in range(n):
                line = f.readline()
                if not line:
                    break
                lines.append(line.rstrip("\n"))
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "..."
        return text
    except OSError:
        return ""


def build_candidate_entry(cf: ConfigFile) -> dict:
    """Build the payload dict for a single candidate file.

    Used by :func:`classify_with_ai` to construct the per-file entry
    sent to the LLM.
    """
    now = datetime.now(tz=timezone.utc)
    try:
        mtime = cf.abs_path.stat().st_mtime
        days_ago = (now - datetime.fromtimestamp(mtime, tz=timezone.utc)).days
    except OSError:
        days_ago = -1

    return {
        "path": str(cf.path),
        "size_bytes": cf.size_bytes,
        "first_lines": _read_first_lines(cf.abs_path),
        "modified_days_ago": days_ago,
    }


def classify_with_ai(
    candidates: list[ConfigFile],
    cfg: DotSyncConfig,
    progress: ProgressCallback | None = None,
) -> list[ConfigFile]:
    """Classify unknown candidates using an LLM via LiteLLM proxy.

    Candidates are sent in batches of :data:`MAX_CANDIDATES_PER_BATCH` to
    avoid context-window overflow.

    Args:
        candidates: ConfigFile objects with include=None to classify.
        cfg: DotSync configuration (needs llm_endpoint set).
        progress: Optional callback for real-time progress events.

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

    system_prompt = SYSTEM_PROMPT

    # Process in batches of MAX_CANDIDATES_PER_BATCH
    total_batches = (len(to_classify) + MAX_CANDIDATES_PER_BATCH - 1) // MAX_CANDIDATES_PER_BATCH
    for batch_idx, batch_start in enumerate(
        range(0, len(to_classify), MAX_CANDIDATES_PER_BATCH)
    ):
        batch = to_classify[batch_start : batch_start + MAX_CANDIDATES_PER_BATCH]
        _emit(progress, {
            "type": "ai_batch",
            "path": None,
            "reason": f"batch {batch_idx + 1} of {total_batches}",
            "count": len(batch),
            "total": total_batches,
        })
        items = [build_candidate_entry(cf) for cf in batch]
        user_message = json.dumps(items)
        batch_paths = [str(cf.path) for cf in batch]

        logger.debug(
            "ai batch %d/%d: %d files, prompt=%d chars, message=%d chars, paths=%s",
            batch_idx + 1, total_batches, len(batch),
            len(system_prompt), len(user_message), batch_paths,
        )

        t0 = time.monotonic()
        try:
            content = chat_completion(
                endpoint=cfg.llm_endpoint,
                model=cfg.llm_model,
                system_prompt=system_prompt,
                user_message=user_message,
                api_key=cfg.llm_api_key,
            )
            elapsed = time.monotonic() - t0

            verdicts_raw = json.loads(content)

            verdict_map: dict[str, str] = {}
            if isinstance(verdicts_raw, list):
                for v in verdicts_raw:
                    if isinstance(v, dict) and "path" in v and "verdict" in v:
                        verdict_map[v["path"]] = v["verdict"]

            logger.debug(
                "ai batch %d/%d: ok in %.1fs, response=%d chars, verdicts=%s",
                batch_idx + 1, total_batches, elapsed, len(content), verdict_map,
            )

            for cf in batch:
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

        except LLMError as exc:
            elapsed = time.monotonic() - t0
            logger.debug(
                "ai batch %d/%d: error after %.1fs: %s",
                batch_idx + 1, total_batches, elapsed, exc,
            )
            _emit(progress, {
                "type": "ai_error",
                "path": None,
                "reason": str(exc),
                "count": None,
            })
            # Mark only this batch as unreachable; continue to next batch
            for cf in batch:
                cf.include = None
                cf.reason = "ai:unreachable"
        except (json.JSONDecodeError, TypeError) as exc:
            elapsed = time.monotonic() - t0
            logger.debug(
                "ai batch %d/%d: parse error after %.1fs: %s, raw=%s",
                batch_idx + 1, total_batches, elapsed, exc, content,
            )
            for cf in batch:
                cf.include = None
                cf.reason = "ask_user"

    _save_classification_cache(cache)
    return candidates


# ---------------------------------------------------------------------------
# Step 2.6 — discover orchestrator
# ---------------------------------------------------------------------------


def discover(
    cfg: DotSyncConfig,
    progress: ProgressCallback | None = None,
) -> list[ConfigFile]:
    """Discover and classify configuration files.

    Scans filesystem roots, applies heuristic classification, optionally
    queries AI for ambiguous files, and returns the full list.

    Args:
        cfg: DotSync configuration.
        progress: Optional callback for real-time progress events.

    Returns:
        List of ConfigFile with classification results.
    """
    _emit(progress, {"type": "phase_start", "reason": "scan", "path": None, "count": None})
    extra = list(cfg.include_extra) if cfg.include_extra else None
    candidates = scan_candidates(extra_paths=extra, repo_path=cfg.repo_path, progress=progress)
    _emit(progress, {"type": "phase_done", "reason": "scan", "path": None, "count": len(candidates)})

    _emit(progress, {"type": "phase_start", "reason": "heuristic", "path": None, "count": None})
    classified = classify_heuristic(candidates, cfg)
    _emit(progress, {"type": "phase_done", "reason": "heuristic", "path": None, "count": len(classified)})

    # Separate ambiguous for AI classification
    ambiguous = [cf for cf in classified if cf.include is None]

    if ambiguous and cfg.llm_endpoint:
        _emit(progress, {"type": "phase_start", "reason": "ai_triage", "path": None, "count": len(ambiguous)})
        classify_with_ai(ambiguous, cfg, progress=progress)
        _emit(progress, {"type": "phase_done", "reason": "ai_triage", "path": None, "count": len(ambiguous)})

    # Any remaining None: if heuristic-matched, fall back to include; otherwise ask_user
    heuristic_reasons = {rule["reason"] for rule in HEURISTIC_RULES}
    for cf in classified:
        if cf.include is None:
            if cf.reason in heuristic_reasons:
                cf.include = True
            else:
                cf.reason = "ask_user"

    return classified
