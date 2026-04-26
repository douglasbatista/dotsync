"""Sensitive data flagging for configuration files.

Scans files marked ``include=True`` by the discovery module for credentials,
API keys, and PEM blocks before they enter the git repository.  Users should
consciously decide about sensitive files.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotsync.config import CONFIG_DIR, DotSyncConfig
from dotsync.discovery import ConfigFile
from dotsync.llm_client import LLMError, chat_completion

# ---------------------------------------------------------------------------
# Constants — compiled regex patterns for secret detection
# ---------------------------------------------------------------------------

SENSITIVE_PATTERNS: dict[str, re.Pattern[str]] = {
    "github_token": re.compile(r"ghp_[A-Za-z0-9]{36}"),
    "github_fine": re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "aws_secret": re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*\S+"),
    "openai_key": re.compile(r"sk-[A-Za-z0-9]{20,}"),
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9-]{20,}"),
    "generic_api_key": re.compile(r"(?i)api[_-]?key\s*[=:]\s*\S+"),
    "generic_token": re.compile(r"(?i)token\s*[=:]\s*\S+"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "private_key_pem": re.compile(r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----"),
    "connection_string": re.compile(
        r"(?i)(mongodb|postgres|mysql|redis|amqp)://\S+"
    ),
}

# Patterns where comment lines (starting with #) should be skipped
_SKIP_COMMENTS: set[str] = {"generic_token", "generic_api_key"}

# ---------------------------------------------------------------------------
# NEVER_INCLUDE — defense-in-depth blocklist
# ---------------------------------------------------------------------------

NEVER_INCLUDE: list[str] = [
    ".ssh/id_rsa",
    ".ssh/id_ed25519",
    ".ssh/id_ecdsa",
    ".gnupg/",
]

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SensitiveMatch:
    """A single secret match found in a file."""

    pattern_name: str
    line_number: int
    preview: str  # redacted: "ghp_ab***xy"


@dataclass
class FlagResult:
    """Result of scanning a single ConfigFile for sensitive data."""

    config_file: ConfigFile
    matches: list[SensitiveMatch] = field(default_factory=list)
    ai_flagged: bool = False
    requires_confirmation: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SENSITIVITY_SYSTEM_PROMPT = (
    "You are a security reviewer. Given a file's path, size, and first lines, "
    "determine if it contains sensitive data (credentials, private keys, tokens, "
    "connection strings). Respond with a JSON object: "
    '{"sensitive": true/false, "reason": "brief explanation"}. '
    "Only output the JSON object, nothing else."
)

SENSITIVITY_CACHE_FILE = CONFIG_DIR / "sensitivity_cache.json"


def _redact(value: str) -> str:
    """Redact a matched value, showing first 2 and last 2 chars.

    For values of 4 chars or fewer, returns ``***`` only.
    """
    if len(value) <= 4:
        return "***"
    return f"{value[:2]}***{value[-2:]}"


# ---------------------------------------------------------------------------
# Core scanning
# ---------------------------------------------------------------------------


def scan_file_for_secrets(path: Path) -> list[SensitiveMatch]:
    """Scan a file for sensitive patterns.

    Reads the file line by line as UTF-8. Silently returns an empty list on
    ``UnicodeDecodeError`` or ``OSError``.

    Args:
        path: Absolute path to the file to scan.

    Returns:
        List of matches found, with redacted previews.
    """
    matches: list[SensitiveMatch] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line_number, line in enumerate(f, start=1):
                stripped = line.strip()
                for name, pattern in SENSITIVE_PATTERNS.items():
                    # Skip comment lines for generic patterns
                    if name in _SKIP_COMMENTS and stripped.startswith("#"):
                        continue
                    m = pattern.search(line)
                    if m:
                        matches.append(
                            SensitiveMatch(
                                pattern_name=name,
                                line_number=line_number,
                                preview=_redact(m.group()),
                            )
                        )
    except (UnicodeDecodeError, OSError):
        return []
    return matches


# ---------------------------------------------------------------------------
# AI flag check
# ---------------------------------------------------------------------------


def _load_sensitivity_cache() -> dict[str, dict]:
    """Load cached AI sensitivity results."""
    if not SENSITIVITY_CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(SENSITIVITY_CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_sensitivity_cache(cache: dict[str, dict]) -> None:
    """Persist AI sensitivity cache to disk."""
    SENSITIVITY_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SENSITIVITY_CACHE_FILE.write_text(
        json.dumps(cache, indent=2),
        encoding="utf-8",
    )


def ai_flag_check(path: Path, cfg: DotSyncConfig) -> bool:
    """Ask the LLM whether a file appears to contain sensitive data.

    Results are cached in ``~/.dotsync/sensitivity_cache.json`` keyed by
    ``"{path}:{mtime}"``.  On any error the function fails open (returns
    ``False``).

    Args:
        path: Absolute path to the file to check.
        cfg: DotSync configuration (needs ``llm_endpoint`` set).

    Returns:
        ``True`` if the AI flags the file as sensitive, ``False`` otherwise.
    """
    if cfg.llm_endpoint is None:
        return False

    # Build cache key
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    cache_key = f"{path}:{mtime}"

    cache = _load_sensitivity_cache()
    if cache_key in cache:
        return cache[cache_key].get("sensitive", False)

    # Read first 30 lines
    first_lines: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for _ in range(30):
                line = f.readline()
                if not line:
                    break
                first_lines.append(line.rstrip("\n"))
        size = path.stat().st_size
    except OSError:
        return False

    payload = json.dumps(
        {"path": str(path), "size_bytes": size, "first_lines": first_lines}
    )

    try:
        content = chat_completion(
            endpoint=cfg.llm_endpoint,
            model=cfg.llm_model,
            system_prompt=SENSITIVITY_SYSTEM_PROMPT,
            user_message=payload,
            api_key=cfg.llm_api_key,
        )
        result = json.loads(content)
        sensitive = bool(result.get("sensitive", False))
    except (LLMError, json.JSONDecodeError, TypeError, KeyError):
        return False

    cache[cache_key] = {"sensitive": sensitive, "reason": result.get("reason", "")}
    _save_sensitivity_cache(cache)
    return sensitive


# ---------------------------------------------------------------------------
# Orchestrators
# ---------------------------------------------------------------------------


def flag_all(files: list[ConfigFile], cfg: DotSyncConfig) -> list[FlagResult]:
    """Scan included files for sensitive data.

    Runs regex-based secret detection on every ``include=True`` file.  If no
    regex matches are found, optionally queries the AI for a sensitivity check
    (to avoid redundant AI calls when regex already found something).

    Args:
        files: ConfigFile objects from the discovery module.
        cfg: DotSync configuration.

    Returns:
        A FlagResult for each included file.
    """
    results: list[FlagResult] = []

    for cf in files:
        if cf.include is not True:
            continue

        matches = scan_file_for_secrets(cf.abs_path)
        ai_flagged = False

        # Only call AI if no regex matches found
        if not matches:
            ai_flagged = ai_flag_check(cf.abs_path, cfg)

        requires_confirmation = bool(matches) or ai_flagged

        results.append(
            FlagResult(
                config_file=cf,
                matches=matches,
                ai_flagged=ai_flagged,
                requires_confirmation=requires_confirmation,
            )
        )

    return results


def enforce_never_include(files: list[ConfigFile]) -> list[ConfigFile]:
    """Enforce the NEVER_INCLUDE blocklist on a list of ConfigFiles.

    Matches each file's relative path against ``NEVER_INCLUDE``.  Entries
    ending with ``/`` match as directory prefixes; others require an exact
    match.  Matched files are set to ``include=False`` with
    ``reason="never_include"``.

    Args:
        files: ConfigFile objects to check.

    Returns:
        The same (mutated) list.
    """
    for cf in files:
        rel_str = str(cf.path)
        for entry in NEVER_INCLUDE:
            if entry.endswith("/"):
                # Directory prefix match
                if rel_str.startswith(entry) or rel_str == entry.rstrip("/"):
                    cf.include = False
                    cf.reason = "never_include"
                    break
            else:
                if rel_str == entry:
                    cf.include = False
                    cf.reason = "never_include"
                    break
    return files
