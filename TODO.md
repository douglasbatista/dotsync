# DotSync — Improvement Plan

Full analysis is in the previous conversation context. This file is designed to be read in isolation so you can clear context and start executing items in priority order.

---

## Legend

| Priority | Meaning |
|---|---|
| P0 | Critical — security, correctness, or testability blocker |
| P1 | Important — bug risk, missing feature, or user-facing rough edge |
| P2 | Architectural — code organization, maintainability |
| P3 | Nice to have — performance, polish |

---

## P0 — Critical

### P0.1 — Extract orchestration logic from `main.py` into testable modules 

**Files:** `src/dotsync/main.py`, new `src/dotsync/orchestrator.py`
**Context:** `main.py` (~500 LOC) mixes Typer CLI plumbing with deep business logic:
- `_run_discover_with_progress()` — wraps `discover()` + progress UI
- `confirm_sensitive_files()` + `_mark_sensitive()` — interactive triage
- `discover` command — sequences scan → classify → flag → confirm → register
- `sync` command — sequences manifest → flag → snapshot → plan → execute → commit → health
- `restore` command — sequences pull → snapshot → plan → execute → health

**Change:**
Create `src/dotsync/orchestrator.py` containing pure functions (no Typer/Rich imports) for each workflow:

```python
def run_discover(cfg: DotSyncConfig) -> DiscoverResult: ...

def run_sync(cfg: DotSyncConfig, *, dry_run: bool, no_push: bool, message: str) -> SyncResult: ...

def run_restore(cfg: DotSyncConfig, *, dry_run: bool, no_pull: bool, from_snapshot: str | None) -> RestoreResult: ...
```

Each returns a dataclass with everything the CLI needs to render (tables, success/warning counts, exit codes) but does **not** print directly.

Then `main.py` becomes thin:
```python
@app.command()
def discover(no_ai: bool = False) -> None:
    cfg = load_config()
    result = orchestrator.run_discover(cfg)
    # render result with Rich
```

**Tests to add:**
- `tests/test_orchestrator.py` — unit tests for each orchestrator function using mocked `discover`, `flag_all`, `create_snapshot`, etc. No subprocess, no real Git repo needed.
- Keep `tests/test_cli.py` for Typer exit-code / argument tests only.

---

## P1 — Important

### P1.1 — Add `py.typed` marker

**Files:** new `src/dotsync/py.typed`
**Context:** The package is fully typed but untyped by default for external `mypy` consumers.
**Change:** Create an empty file at `src/dotsync/py.typed` and ensure it is packaged (Hatchling includes it automatically if present, but verify).

---

### P1.2 — Fix `transform_paths` robustness or scope-limit it

**File:** `src/dotsync/sync.py` (`transform_paths()`) 
**Context:** Regex-based path rewriting across platforms is fragile:
- URL protection is heuristic (`http` in 5 preceding chars).
- It can mangle JSON-escaped paths, TOML inline tables, registry files, and XML attributes.
- It has a dead branch in `execute_restore` (inner `if` checking `None` after `can_transform` already proved non-None).

**Change:**
1. **Remove the dead branch** in `execute_restore`.
2. **Limit transforms** to an allowlist of file extensions where there is a known need: `.json`, `.yaml`, `.yml`, `.toml`, `.ini`, `.conf`, `.cfg`.
3. For **JSON** files, parse with `json.loads` → walk & mutate string values containing `source_home` → `json.dumps` (preserve indentation where possible, or document that whitespace may change).
4. For **YAML/TOML/INI**, keep the regex but tighten it:
   - Use a proper negative lookbehind for URLs: `(?<!http)(?<!https)(?<!ftp)` before the `:` match.
   - Only transform values that look like file paths (contain `/` or `\`).
5. Document in docstring that transform is **best-effort** for non-structured formats.

**Tests to add:**
- JSON round-trip preserving order: `"editor": "/home/user/bin/nvim"` → `"editor": "C:\\Users\\user\\bin\\nvim"`
- URL is *not* mangled when it contains the home path as a query param.
- Dead branch removal confirmed by coverage.

---

### P1.3 — Introduce `DotSyncError` base exception

**Files:** new `src/dotsync/errors.py`, then `config.py`, `git_ops.py`, `health.py`, `snapshot.py`, `llm_client.py`
**Context:** There are ~8 independent exception classes. A CLI catch-all cannot easily distinguish user-facing errors (e.g. missing config, merge conflict) from programming bugs.
**Change:**
1. Create `src/dotsync/errors.py`:
   ```python
   class DotSyncError(Exception):
       """Base class for all user-facing, expected DotSync errors."""
   ```
2. Make every custom exception inherit from it:
   - `ConfigNotFoundError(DotSyncError)`
   - `MissingDependencyError(DotSyncError)`
   - `NoRemoteConfiguredError(DotSyncError)`
   - `MergeConflictError(DotSyncError)`
   - `HealthCheckFailedError(DotSyncError)`
   - `SnapshotNotFoundError(DotSyncError)`
   - `LLMError(DotSyncError)`
3. In `main.py`, replace the broad `except Exception` around push with `except DotSyncError` where appropriate, or at minimum add a top-level handler that prints a clean message for `DotSyncError` and a traceback for anything else.

**Tests to add:**
- `tests/test_errors.py` asserting that all listed exceptions are instances of `DotSyncError`.

---

## P2 — Architectural

### P2.1 — Split `discovery.py` into a subpackage

**Files:** `src/dotsync/discovery/`
**Context:** `discovery.py` is ~600 LOC with distinct responsibilities: scanning, heuristics, AI triage, caching, and progress events.
**Change:**
```
src/dotsync/discovery/
├── __init__.py          # re-exports discover(), ConfigFile, ScanEvent, ProgressCallback
├── scanner.py           # scan_candidates(), _scan_dir(), _should_prune_dir(), _prefilter_file()
├── heuristics.py        # classify_heuristic(), _matches_heuristic(), HEURISTIC_RULES
├── ai_triage.py         # classify_with_ai(), build_candidate_entry(), SYSTEM_PROMPT, cache helpers
└── models.py            # ConfigFile, ScanEvent, ProgressCallback
```

Update imports in `main.py` and tests. No behavioral changes.

**Tests to move/update:**
- Move `tests/test_discovery.py` tests into matching classes by submodule (or keep the single file if you prefer, just update import paths).

---

### P2.2 — Atomic snapshot index writes

**File:** `src/dotsync/snapshot.py`
**Context:** `save_index()` does a non-atomic write. Concurrent `sync` processes (cron + manual) can corrupt `snapshot_index.json`.
**Change:**
```python
def save_index(entries: list[SnapshotMeta]) -> None:
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    index_path = SNAPSHOTS_DIR / INDEX_FILENAME
    tmp_path = index_path.with_suffix(".tmp")
    data = [asdict(e) for e in entries]
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(str(tmp_path), str(index_path))
```

---

### P2.3 — Enforce `NEVER_INCLUDE` before AI / interactive steps

**File:** `src/dotsync/orchestrator.py` (or `main.py` if P0.2 not yet done)
**Context:** `NEVER_INCLUDE` (private keys, `.gnupg/`, etc.) is currently enforced **after** AI classification and user confirmation. This means an SSH private key could be shown to the LLM and prompted to the user before being excluded.
**Change:**
Move `enforce_never_include(files)` to run immediately after `scan_candidates()` / heuristic classification, **before** `classify_with_ai()` or the pending-file confirmation loop. The flow should be:

```
scan → heuristic classify → NEVER_INCLUDE enforcement → AI triage (remaining ambiguous) → user confirm
```

**Tests to add:**
- A test where `.ssh/id_rsa` is created; assert it never reaches `chat_completion` mock and never appears in the pending prompt list.

---

## P3 — Nice to Have

### P3.1 — Parallel health checks

**File:** `src/dotsync/health.py`
**Context:** `run_all_checks()` iterates sequentially. They are independent subprocess calls that could run in parallel.
**Change:**
Wrap the loop in a `ThreadPoolExecutor`:
```python
with ThreadPoolExecutor(max_workers=min(len(enabled), 4)) as executor:
    results = list(executor.map(run_check, enabled))
```
Keep execution order deterministic for stable output.

**Tests:** Existing tests already mock `subprocess.run`; ensure they still pass (each test mocks globally, so parallelizing the loop is transparent to mocks).

---

### P3.2 — Improve `config --set` type reflection

**File:** `src/dotsync/main.py` (`config` command)
**Context:** Type coercion uses string checks like `"list" in str(field_type).lower()` which is brittle.
**Change:**
Use the typing module:
```python
from typing import get_origin, get_args

origin = get_origin(field_type)
if origin is list:
    args = get_args(field_type)
    # args[0] is the element type: str or Path
```

Also handle `str | None` (Pydantic v2 annotations come through as `str | None`, not `Optional[str]` in all cases) by checking for `types.UnionType` and whether `None` is a union member.

**Tests to add:**
- `config --set repo_path=/tmp/foo` → `Path`
- `config --set snapshot_keep=10` → `int`
- `config --set health_checks=git\ status,echo\ hi` → `list[str]`
- `config --set include_extra=/tmp/a,/tmp/b` → `list[Path]`

---

### P3.3 — Cap `ThreadPoolExecutor` worker count in `scan_candidates`

**File:** `src/dotsync/discovery/scanner.py` (or `discovery.py` if P2.1 not yet done)
**Change:**
```python
max_workers=min(len(roots), 4)
```

---

### P3.4 — Deduplicate first-line reading between `discovery` and `flagging`

**File:** `src/dotsync/discovery/ai_triage.py` (or `discovery.py`) and `src/dotsync/flagging.py`
**Context:** `ai_flag_check()` in `flagging.py` manually reads 30 lines; `_read_first_lines()` in `discovery.py` reads 5 lines with truncation. Extract a shared helper in a new `src/dotsync/utils.py` or keep it in `discovery` and import into `flagging`.

**Shared signature:**
```python
def read_first_lines(path: Path, n: int = 5, max_chars: int = 200) -> str: ...
```

---

### P3.5 — Snapshot rollback should use stored file list instead of `rglob`

**File:** `src/dotsync/snapshot.py` (`rollback()`)
**Context:** `rollback()` iterates over the directory tree with `rglob("*")`, skipping directories. It is slower and non-deterministic compared to using the manifest.
**Change:**
- Store the file list (relative paths) inside `SnapshotMeta` or in a `.manifest.json` inside each snapshot directory during `create_snapshot()`.
- `rollback()` should restore exactly that list rather than scanning.
- This also protects against the edge case where a snapshot directory has extra files dropped in by accident.

**Note:** If `create_snapshot()` is changed, ensure `rollback_latest()` and verify tests still pass.

---

## Done Criteria

When implementing an item:
1. Do the code change.
2. Update or add tests for the change.
3. Run the full CI pipeline:
   ```bash
   uv sync --frozen
   uv run ruff check . && uv run ruff format .
   uv run mypy src/
   uv run pytest
   ```
4. Mark the task as complete in this file (append `✅` or move to a "Done" section).
