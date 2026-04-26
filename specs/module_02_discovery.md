# Module 02 — Discovery & Classification

## Responsibility

Scan known filesystem locations for candidate configuration files, apply heuristic-based filtering to produce an initial include/exclude verdict, and optionally pass ambiguous candidates to an AI agent for triage. Output is a list of `ConfigFile` objects ready for user review.

---

## Step 2.1 — Exclude lists and heuristic rules

### `SAFETY_EXCLUDES` (list)

Paths matching these patterns are **never** included — security invariants:

```
.ssh/id_*
.ssh/id_*.pub
.gnupg/
.dotsync/
```

### `PRUNE_DIRS` / `_PRUNE_PREFIXES`

Directories pruned during scanning — by exact name match or prefix:

- **PRUNE_DIRS**: ~33 directory names (`.git`, `node_modules`, `__pycache__`, cache dirs, build dirs, IDE state, AI agent state, etc.)
- **_PRUNE_PREFIXES**: `.local/share/`, `.local/lib/`

### Whitelist constants

```python
ALLOWED_EXTENSIONS: frozenset[str]  # 14 config extensions (.toml, .yaml, .json, .ini, .cfg, .conf, .xml, .env, .rc, .plist, etc.)
ALLOWED_NAMED_FILES: frozenset[str]  # extensionless names: "config", "credentials"
HOME_BLOCKED_DOTFILES: frozenset[str]  # ~18 known noise dotfiles (.bash_history, .viminfo, .lesshst, etc.)
```

### `HEURISTIC_RULES` (list[dict])

Structural rules evaluated in order; first match wins:

| Pattern | max_depth | Reason | Extensions |
|---|---|---|---|
| `is_home_dotfile` | 1 | home dotfile | — |
| `under_config_dir` | 3 | XDG config | — |
| `windows_appdata` | 4 | Windows app config | .json .toml .yaml .yml .ini .conf .xml .cfg |
| `config_extension` | 2 | config extension | .toml .yaml .yml .ini .conf .cfg |

**Depth model:** count path parts after the anchor directory.

- `is_home_dotfile`: exactly 1 part, starts with `.`
- `under_config_dir`: starts with `.config/`, `(n_parts - 1) <= max_depth`
- `windows_appdata`: contains `AppData`, `(n_parts - appdata_idx - 1) <= max_depth`, has config extension
- `config_extension`: `(n_parts - 1) <= max_depth`, has config extension

### Constants

- `MAX_DEPTH = 5` — hard ceiling for filesystem walk
- `MAX_FILE_SIZE = 50_000` — skip files larger than 50 KB
- `BINARY_CHECK_BYTES = 512` — bytes to read for binary detection

### Tests

| Test | Assertion |
|---|---|
| `test_safety_excludes_are_all_relative_paths` | No entry starts with `/` |
| `test_safety_excludes_do_not_overlap_scan_excludes` | No overlap between lists |
| `test_heuristic_rules_have_required_keys` | Every rule has pattern, max_depth, reason |

---

## Step 2.2 — Filesystem scanner

### `scan_candidates(extra_paths=None) -> list[Path]`

Walk each root returned by `config_dirs()`:

| Rule | Detail |
|---|---|
| No symlink following | `followlinks=False`; skip symlink dirs and files |
| Max depth | 5 levels below each root |
| Dir pruning | `PRUNE_DIRS` (name), `_PRUNE_PREFIXES` (prefix), safety excludes, `repo_path`, generated dir names |
| Safety excludes | `fnmatch` against `SAFETY_EXCLUDES` for files |
| Size gate | Skip files > 50 KB |
| Whitelist gate | Accept `ALLOWED_EXTENSIONS`, `ALLOWED_NAMED_FILES`, extensionless home dotfiles; reject `HOME_BLOCKED_DOTFILES` |
| Binary gate | Read first 512 bytes; skip if `\x00` found (runs last — only for whitelist-passing files) |
| Extra paths | Append `extra_paths` if they exist on disk **and pass `SAFETY_EXCLUDES`** (bypass whitelist) |
| Dedup | By `Path.resolve()` |

Returns a deduplicated `list[Path]` of absolute paths.

### Tests

| Test | Assertion |
|---|---|
| `test_scan_excludes_ssh_private_keys` | `.ssh/id_rsa` filtered, `.ssh/config` kept |
| `test_scan_skips_large_files` | >512 KB file excluded |
| `test_scan_skips_binary_files` | File with null bytes excluded |
| `test_scan_respects_hard_max_depth` | Depth > 5 excluded |
| `test_scan_includes_extra_paths` | Extra path appended |
| `test_scan_excludes_gnupg_dir` | `.gnupg/` contents excluded |
| `test_scan_extra_paths_still_respect_safety_excludes` | SSH key via extra_paths rejected |

---

## Step 2.3 — Heuristic classifier

### `classify_heuristic(candidates, cfg) -> list[ConfigFile]`

For each candidate path:

1. If it matches a user `exclude_patterns` glob -> `include=False, reason="user_excluded"`
2. If it matches `include_extra` -> `include=True, reason="user_included"`
3. If it matches a `HEURISTIC_RULES` entry (first match wins) -> `include=None, reason=<rule reason>` (tags reason but defers verdict to AI)
4. Otherwise -> `include=None, reason="ambiguous"`

Heuristics no longer auto-include files. They tag the matching reason but leave `include=None` so the AI triage agent has final say. When no AI endpoint is configured, `discover()` falls back to `include=True` for heuristic-matched files.

`os_profile` detection:

- `"windows"` if path contains `AppData`
- `"linux"` if path starts with `.config` or contains `/home/`
- `"shared"` otherwise

### Tests

| Test | Assertion |
|---|---|
| `test_home_dotfile_tagged` | `.gitconfig` -> `include=None, reason="home dotfile"` |
| `test_xdg_config_tagged` | `.config/nvim/init.lua` -> `include=None, reason="XDG config"` |
| `test_windows_appdata_json_tagged` | `AppData/.../settings.json` depth 4 -> `include=None, reason="Windows app config"` |
| `test_windows_appdata_too_deep_excluded` | Depth 5 -> `include=None` |
| `test_user_exclude_overrides_heuristic` | Pattern match -> `include=False` |
| `test_ambiguous_file_pending` | `.log` file -> `include=None, reason="ambiguous"` |
| `test_os_profile_windows` | `AppData/...` -> `os_profile="windows"` |
| `test_os_profile_linux` | `.config/...` -> `os_profile="linux"` |

---

## Step 2.4 — AI triage agent

### `classify_with_ai(candidates, cfg, progress=None) -> list[ConfigFile]`

Called for all files where `include is None` (both heuristic-matched and truly ambiguous).

1. Load classification cache from `~/.dotsync/classification_cache.json`
2. For cached entries, apply cached verdict
3. For uncached entries, build payload with `path`, `size_bytes`, `first_lines` (joined string), `modified_days_ago`
4. Process in batches of `MAX_CANDIDATES_PER_BATCH` (10)
5. Call `chat_completion()` from `llm_client` with system prompt requesting `path`, `verdict`, and `reason` fields
6. Parse JSON array response, map verdicts:
   - `"include"` -> `include=True, reason="ai:include"`
   - `"exclude"` -> `include=False, reason="ai:exclude"`
   - anything else -> `include=None, reason="ask_user"`
7. Save results to cache
8. On `LLMError`, mark only the current batch as `reason="ai:unreachable"` and continue to next batch (do not abandon remaining batches)
9. On parse failure (`JSONDecodeError`), mark batch as `reason="ask_user"` and continue
10. All batch details (paths, timing, verdicts, errors) logged at DEBUG level via `logging.getLogger("dotsync")`

### Tests

| Test | Assertion |
|---|---|
| `test_ai_classify_returns_valid_verdicts` | `include` verdict -> `include=True` |
| `test_ai_classify_fallback_on_error` | `LLMError` -> `reason="ask_user"` |
| `test_ai_classify_uses_cache` | Cached entry skips API call |
| `test_ai_classify_saves_to_cache` | New verdict persisted to cache |
| `test_classify_with_ai_chunks_large_input` | 45 candidates -> 5 batches of 10 |
| `test_classify_with_ai_continues_after_batch_failure` | Failed batch 2 doesn't prevent batch 3 from running |

---

## Step 2.5 — OpenAI-compatible HTTP client wrapper

### File: `src/dotsync/llm_client.py`

### `LLMError(Exception)`

Raised on HTTP error, timeout, or malformed response.

### `chat_completion(endpoint, model, system_prompt, user_message, timeout=90, max_retries=2) -> str`

Send a chat-completion request to `{endpoint}/v1/chat/completions` and return the assistant content string.

- `timeout` is a positional parameter (type `int`, default `90`)
- `max_retries` controls retry attempts on transient errors (default `2`)
- Uses `httpx.post` with `temperature=0`
- Retries with exponential backoff (`2 ** attempt` seconds: 2s, 4s) on:
  - HTTP status errors (`httpx.HTTPStatusError`)
  - Timeout (`httpx.TimeoutException`)
  - Other HTTP errors (`httpx.HTTPError`)
- Does NOT retry on malformed response (`KeyError`/`IndexError`/`TypeError`) — raises `LLMError` immediately
- After exhausting retries, raises the last `LLMError`

### Tests (`tests/test_llm_client.py`)

| Test | Assertion |
|---|---|
| `test_chat_completion_returns_content_string` | Successful response returns content |
| `test_chat_completion_raises_llm_error_on_http_error` | HTTP 500 -> `LLMError` (with `max_retries=0`) |
| `test_chat_completion_raises_llm_error_on_timeout` | Timeout -> `LLMError` (with `max_retries=0`) |
| `test_chat_completion_accepts_positional_timeout` | Timeout as 5th positional arg works |
| `test_chat_completion_raises_llm_error_on_missing_choices` | Malformed body -> `LLMError` immediately (no retry) |
| `test_retries_on_timeout_then_succeeds` | 2 timeouts + success -> 3 calls, correct result |
| `test_retries_on_http_error_then_succeeds` | HTTP 503 + success -> 2 calls, correct result |
| `test_exhausts_retries_then_raises` | Always timeout -> `LLMError` after 3 calls |
| `test_no_retry_on_malformed_response` | Malformed -> `LLMError` after 1 call, no sleep |
| `test_backoff_timing` | Sleep called with 2 then 4 |

---

## Step 2.6 — Discovery orchestrator

### `discover(cfg, progress=None) -> list[ConfigFile]`

1. `scan_candidates(extra_paths=cfg.include_extra, repo_path=cfg.repo_path)`
2. `classify_heuristic(candidates, cfg)` — tags reason but leaves `include=None`
3. Filter unresolved (`include is None`) — includes both heuristic-matched and truly ambiguous
4. If `cfg.llm_endpoint` set, call `classify_with_ai(unresolved, cfg)`
5. Any remaining `include is None` with a heuristic reason -> `include=True` (fallback when AI unavailable or batch failed)
6. Any remaining `include is None` without heuristic reason -> `reason="ask_user"`
7. Return full list

### Tests

| Test | Assertion |
|---|---|
| `test_discover_returns_config_file_list` | Returns `list[ConfigFile]`, `.bashrc` included via fallback |
| `test_discover_skips_ai_when_no_endpoint` | No endpoint -> AI not called |
| `test_discover_never_returns_reason_unknown` | No `reason="unknown"` or `"ambiguous"` in output |
| `test_discover_excludes_ssh_private_keys_end_to_end` | SSH keys never in output |
| `test_discover_ai_receives_all_unresolved` | Both heuristic-matched and ambiguous files sent to AI |
| `test_discover_heuristic_fallback_without_ai` | No AI -> heuristic-matched files fall back to `include=True` |

---

## Acceptance criteria

- [ ] `discover()` returns `list[ConfigFile]` — no `None` verdicts remain
- [ ] SSH private keys (`.ssh/id_*`) never appear in output
- [ ] AI triage receives all unresolved files (heuristic-matched and ambiguous)
- [ ] Failed AI batch does not prevent remaining batches from processing
- [ ] Heuristic-matched files fall back to `include=True` when AI is unavailable
- [ ] Classification cache persists across runs
- [ ] All tests pass: `uv run pytest tests/test_discovery.py tests/test_llm_client.py -v`
