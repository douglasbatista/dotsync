"""Configuration schema and load/save functions for DotSync."""

from pathlib import Path

import tomli_w
import tomllib
from pydantic import BaseModel, Field


class ConfigNotFoundError(Exception):
    """Raised when the configuration file is missing."""

    pass


class DotSyncConfig(BaseModel):
    """Configuration schema for DotSync."""

    repo_path: Path = Field(..., description="Path to the Git repository for storing dotfiles")
    remote_url: str | None = Field(None, description="Optional remote Git repository URL")
    gitcrypt_key_path: Path | None = Field(None, description="Path to git-crypt symmetric key file")
    llm_endpoint: str | None = Field(None, description="LiteLLM proxy endpoint for AI triage")
    llm_model: str = Field("claude-haiku-4-5", description="LLM model to use for AI triage")
    snapshot_keep: int = Field(5, description="Number of local snapshots to retain")
    health_checks: list[str] = Field(default_factory=list, description="List of health check commands")
    exclude_patterns: list[str] = Field(default_factory=list, description="Glob patterns to exclude from sync")
    include_extra: list[str] = Field(default_factory=list, description="Additional paths to include in sync")


CONFIG_DIR = Path.home() / ".dotsync"
CONFIG_FILE = CONFIG_DIR / "config.toml"


def default_config() -> DotSyncConfig:
    """Return a configuration with default values."""
    return DotSyncConfig(
        repo_path=Path.home() / "dotsync-repo",
        remote_url=None,
        gitcrypt_key_path=None,
        llm_endpoint=None,
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

    with CONFIG_FILE.open("rb") as f:
        data = tomllib.load(f)

    return DotSyncConfig.model_validate(data)


def _serialize_for_toml(cfg: DotSyncConfig) -> dict:
    """Convert config to TOML-serializable dict.

    Path objects are converted to strings since tomli_w doesn't support them.
    None values are omitted since TOML doesn't support null.
    """
    data = cfg.model_dump()
    result: dict = {}
    for key, value in data.items():
        if value is None:
            continue  # Skip None values - TOML doesn't support null
        if isinstance(value, Path):
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
