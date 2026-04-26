"""Configuration schema and load/save functions for DotSync."""

import os
import re
from pathlib import Path

import tomli_w
import tomllib
from pydantic import BaseModel, Field, field_validator

_ENV_TOKEN = re.compile(r"\{env:([^}]+)\}")


def _substitute_env(value: str) -> str:
    """Replace ``{env:VAR_NAME}`` tokens with the corresponding environment variable.

    Raises:
        ValueError: If a referenced environment variable is not set.
    """
    def _replace(m: re.Match) -> str:
        var = m.group(1)
        val = os.environ.get(var)
        if val is None:
            raise ValueError(f"Environment variable '{var}' referenced in config is not set")
        return val

    return _ENV_TOKEN.sub(_replace, value)


class ConfigNotFoundError(Exception):
    """Raised when the configuration file is missing."""

    pass


def expand_path(p: str | Path | None, resolve: bool = True) -> Path | None:
    """Expand ~ and %USERPROFILE% / $HOME env vars, then optionally resolve to absolute path."""
    if p is None:
        return None
    expanded = Path(os.path.expandvars(str(p))).expanduser()
    return expanded.resolve() if resolve else expanded


class DotSyncConfig(BaseModel):
    """Configuration schema for DotSync."""

    repo_path: Path = Field(..., description="Path to the Git repository for storing dotfiles")
    remote_url: str | None = Field(None, description="Optional remote Git repository URL")
    llm_endpoint: str | None = Field(None, description="LiteLLM proxy endpoint for AI triage")
    llm_api_key: str | None = Field(None, description="Bearer token for the LLM endpoint")
    llm_model: str = Field("claude-haiku-4-5", description="LLM model to use for AI triage")
    snapshot_keep: int = Field(5, description="Number of local snapshots to retain")
    health_checks: list[str] = Field(default_factory=list, description="List of health check commands")
    exclude_patterns: list[str] = Field(default_factory=list, description="Glob patterns to exclude from sync")
    include_extra: list[Path] = Field(default_factory=list, description="Additional paths to include in sync")

    @field_validator("llm_endpoint", "llm_api_key", "llm_model", mode="before")
    @classmethod
    def expand_llm_env_vars(cls, v: str | None) -> str | None:
        """Substitute ``{env:VAR}`` tokens in LLM config fields."""
        if not isinstance(v, str):
            return v
        return _substitute_env(v)

    @field_validator("repo_path", mode="before")
    @classmethod
    def expand_single_path(cls, v: str | Path | None) -> Path | None:
        """Expand ~ and env vars for single path fields."""
        return expand_path(v, resolve=True)

    @field_validator("include_extra", mode="before")
    @classmethod
    def expand_path_list(cls, v: list[str | Path] | None) -> list[Path]:
        """Expand ~ and env vars for each path in the list."""
        return [expand_path(p, resolve=True) for p in (v or [])]  # type: ignore[misc]

    @field_validator("exclude_patterns", mode="before")
    @classmethod
    def expand_pattern_list(cls, v: list[str] | None) -> list[str]:
        """Expand ~ and env vars in patterns without resolving."""
        if not v:
            return []
        return [str(Path(os.path.expandvars(p)).expanduser()) for p in v]


CONFIG_DIR = Path.home() / ".dotsync"
CONFIG_FILE = CONFIG_DIR / "config.toml"


def default_config() -> DotSyncConfig:
    """Return a configuration with default values."""
    return DotSyncConfig(
        repo_path=Path.home() / "dotsync-repo",
        remote_url=None,
        llm_endpoint=None,
        llm_api_key=None,
        llm_model="claude-haiku-4-5",
        snapshot_keep=5,
        health_checks=[],
        exclude_patterns=[],
        include_extra=[],
    )


def load_config() -> DotSyncConfig:
    """Load configuration from ~/.dotsync/config.toml.

    Raises:
        ConfigNotFoundError: If the configuration file does not exist.

    Returns:
        DotSyncConfig: The loaded configuration.
    """
    if not CONFIG_FILE.exists():
        raise ConfigNotFoundError(f"Configuration file not found: {CONFIG_FILE}")

    _OPTIONAL_FIELDS = {"remote_url", "llm_endpoint", "llm_api_key"}

    with CONFIG_FILE.open("rb") as f:
        data = tomllib.load(f)

    for field in _OPTIONAL_FIELDS:
        if field in data and data[field] == "":
            data[field] = None

    return DotSyncConfig.model_validate(data)


def _serialize_for_toml(cfg: DotSyncConfig) -> dict:
    """Convert config to TOML-serializable dict.

    Path objects are converted to strings since tomli_w doesn't support them.
    None values are written as empty strings since TOML doesn't support null.
    """
    data = cfg.model_dump()
    result: dict = {}
    for key, value in data.items():
        if value is None:
            result[key] = ""
        elif isinstance(value, Path):
            result[key] = str(value)
        elif isinstance(value, list):
            result[key] = [str(v) if isinstance(v, Path) else v for v in value]
        else:
            result[key] = value
    return result


def save_config(cfg: DotSyncConfig) -> None:
    """Save configuration to ~/.dotsync/config.toml.

    Creates the config directory if it doesn't exist.

    Args:
        cfg: The configuration to save.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    with CONFIG_FILE.open("wb") as f:
        tomli_w.dump(_serialize_for_toml(cfg), f)
