# Module 01 — Core / Bootstrap

## Responsibility

Project scaffolding, uv setup, CLI entry point skeleton, configuration schema loading/saving, and logging infrastructure. Every other module depends on this one.

---

## Incremental Steps

### Step 1.1 — Initialize uv project

**Goal:** Runnable project with correct metadata.

```bash
uv init dotsync
cd dotsync
uv add typer rich pydantic httpx gitpython
uv add --dev pytest pytest-tmp-path
```

`pyproject.toml` must declare:
- `[project.scripts] dotsync = "dotsync.main:app"`
- Python `>=3.12`

**Test:** `uv run dotsync --help` prints usage without error.

---

### Step 1.2 — CLI entry point skeleton

**Goal:** Typer app with no-op command stubs for all top-level commands.

File: `src/dotsync/main.py`

```python
import typer
app = typer.Typer(name="dotsync", help="Config file backup, sync, and restore.")

@app.command()
def init(): ...

@app.command()
def sync(): ...

@app.command()
def restore(): ...

@app.command()
def rollback(): ...

@app.command()
def status(): ...
```

**Test:** Each command runs without error and prints "not implemented yet" via `typer.echo`.

---

### Step 1.3 — Config schema (Pydantic)

**Goal:** Load, validate, and save `~/.dotsync/config.toml`.

File: `src/dotsync/config.py`

Fields:
```
repo_path: Path
remote_url: str | None = None
gitcrypt_key_path: Path | None = None
llm_endpoint: str | None = None
llm_model: str = "gpt-4o-mini"  # any model name accepted by the configured endpoint
snapshot_keep: int = 5
health_checks: list[str] = []
exclude_patterns: list[str] = []
include_extra: list[str] = []
```

Functions:
- `load_config() -> DotSyncConfig` — reads `~/.dotsync/config.toml`, raises `ConfigNotFoundError` if missing
- `save_config(cfg: DotSyncConfig) -> None` — writes to `~/.dotsync/config.toml`, creating dir if needed
- `default_config() -> DotSyncConfig` — returns a config with sensible defaults

Format: TOML via `tomllib` (stdlib in 3.11+) for reading, `tomli-w` for writing.

```
uv add tomli-w
```

**Tests:**
- `test_config_roundtrip` — save then load, values match
- `test_config_defaults` — default config has correct types and values
- `test_config_missing` — `load_config()` raises `ConfigNotFoundError` when file absent

---

### Step 1.4 — Logging infrastructure

**Goal:** Structured, leveled logging routed through Rich for pretty console output and a plain file for debugging.

File: `src/dotsync/logging_setup.py`

- Use Python stdlib `logging` with two handlers:
  - `RichHandler` (Rich) for console — INFO and above by default
  - `FileHandler` writing to `~/.dotsync/dotsync.log` — DEBUG and above always
- `setup_logging(verbose: bool = False)` — called once at CLI startup; sets console to DEBUG if verbose
- All modules use `logger = logging.getLogger("dotsync")` — single logger name for unified output

**Test:**
- `test_log_file_created` — after `setup_logging()`, log file exists at expected path
- `test_verbose_flag` — console handler level is DEBUG when `verbose=True`

---

### Step 1.5 — Platform detection utility

**Goal:** Reliable OS detection used by all other modules.

File: `src/dotsync/platform_utils.py`

```python
def current_os() -> Literal["linux", "windows"]
def is_wsl() -> bool   # checks /proc/version for Microsoft
def home_dir() -> Path  # platform-correct home
def config_dirs() -> list[Path]  # default scan roots for current OS
```

`config_dirs()` returns:
- Linux: `[Path.home(), Path.home() / ".config"]`
- Windows: `[Path.home(), Path(os.environ["APPDATA"]), Path(os.environ["LOCALAPPDATA"])]`

**Tests:**
- `test_current_os_returns_valid_literal`
- `test_home_dir_exists`
- `test_config_dirs_all_exist` — filter to existing only, assert non-empty

---

### Step 1.6 — Wire logging and config into CLI

**Goal:** Every CLI command loads config and initializes logging before doing anything.

Pattern:
```python
@app.callback()
def startup(verbose: bool = False):
    setup_logging(verbose)
    # config loaded lazily per command that needs it
```

Add `--verbose / -v` flag to the Typer app callback.

**Test:** `uv run dotsync --verbose status` produces DEBUG output on console.

---

## Acceptance Criteria for Module 01

- [ ] `uv run dotsync --help` lists all commands
- [ ] `~/.dotsync/config.toml` created with defaults on first `init`
- [ ] Config roundtrips correctly through TOML
- [ ] Log file appears at `~/.dotsync/dotsync.log` after any command
- [ ] Platform detection returns correct values on both Windows and Linux
- [ ] All unit tests pass: `uv run pytest tests/test_core.py`
