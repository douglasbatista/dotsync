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
dotsync.key
```

### `SCAN_EXCLUDES` (list)

Directories skipped during scanning to avoid noise:

```
.cache/
.local/share/
.local/lib/
node_modules/
__pycache__/
.git/
.venv/
venv/
.tox/
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
- `MAX_FILE_SIZE = 512_000` — skip files larger than 512 KB

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
| Size gate | Skip files > 512 KB |
| Binary gate | Read first 8 KB; skip if `\x00` found |
| Safety excludes | `fnmatch` against `SAFETY_EXCLUDES` for files |
| Scan excludes | Filter `dirnames` against `SCAN_EXCLUDES` to prune noise dirs |
| Extra paths | Append `extra_paths` if they exist on disk **and pass `SAFETY_EXCLUDES`** |
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
3. If it matches a `HEURISTIC_RULES` entry (first match wins) -> `include=True, reason=<rule reason>`
4. Otherwise -> `include=None, reason="ambiguous"`

`os_profile` detection:

- `"windows"` if path contains `AppData`
- `"linux"` if path starts with `.config` or contains `/home/`
- `"shared"` otherwise

### Tests

| Test | Assertion |
|---|---|
| `test_home_dotfile_included` | `.gitconfig` -> `include=True, reason="home dotfile"` |
| `test_xdg_config_included` | `.config/nvim/init.lua` -> `include=True, reason="XDG config"` |
| `test_windows_appdata_json_included` | `AppData/.../settings.json` depth 4 -> match |
| `test_windows_appdata_too_deep_excluded` | Depth 5 -> `include=None` |
| `test_user_exclude_overrides_heuristic` | Pattern match -> `include=False` |
| `test_ambiguous_file_pending` | `.log` file -> `include=None, reason="ambiguous"` |
| `test_os_profile_windows` | `AppData/...` -> `os_profile="windows"` |
| `test_os_profile_linux` | `.config/...` -> `os_profile="linux"` |

---

## Step 2.4 — AI triage agent

### `classify_with_ai(candidates, cfg) -> list[ConfigFile]`

Only called for files where `include is None`.

1. Load classification cache from `~/.dotsync/classification_cache.json`
2. For cached entries, apply cached verdict
3. For uncached entries, build payload with `path`, `size_bytes`, `first_lines` (joined string), `modified_days_ago`
4. Call `chat_completion()` from `llm_client` with system prompt requesting `path`, `verdict`, and `reason` fields
5. Parse JSON array response, map verdicts:
   - `"include"` -> `include=True, reason="ai:include"`
   - `"exclude"` -> `include=False, reason="ai:exclude"`
   - anything else -> `include=None, reason="ask_user"`
6. Save results to cache
7. On `LLMError` or parse failure, fall back to `reason="ask_user"`

### Tests

| Test | Assertion |
|---|---|
| `test_ai_classify_returns_valid_verdicts` | `include` verdict -> `include=True` |
| `test_ai_classify_fallback_on_error` | `LLMError` -> `reason="ask_user"` |
| `test_ai_classify_uses_cache` | Cached entry skips API call |
| `test_ai_classify_saves_to_cache` | New verdict persisted to cache |

---

## Step 2.5 — OpenAI-compatible HTTP client wrapper

### File: `src/dotsync/llm_client.py`

### `LLMError(Exception)`

Raised on HTTP error, timeout, or malformed response.

### `chat_completion(endpoint, model, system_prompt, user_message, timeout=15) -> str`

Send a chat-completion request to `{endpoint}/v1/chat/completions` and return the assistant content string.

- `timeout` is a positional parameter (type `int`, default `15`)
- Uses `httpx.post` with `temperature=0`
- Raises `LLMError` on:
  - HTTP status errors
  - Timeout
  - Missing `choices[0].message.content` in response

### Tests (`tests/test_llm_client.py`)

| Test | Assertion |
|---|---|
| `test_chat_completion_returns_content_string` | Successful response returns content |
| `test_chat_completion_raises_llm_error_on_http_error` | HTTP 500 -> `LLMError` |
| `test_chat_completion_raises_llm_error_on_timeout` | Timeout -> `LLMError` |
| `test_chat_completion_accepts_positional_timeout` | Timeout as 5th positional arg works |
| `test_chat_completion_raises_llm_error_on_missing_choices` | Malformed body -> `LLMError` |

---

## Step 2.6 — Discovery orchestrator

### `discover(cfg) -> list[ConfigFile]`

1. `scan_candidates(extra_paths=cfg.include_extra)`
2. `classify_heuristic(candidates, cfg)`
3. Filter ambiguous (`include is None`)
4. If `cfg.llm_endpoint` set, call `classify_with_ai(ambiguous, cfg)`
5. Any remaining `include is None` -> `reason="ask_user"`
6. Return full list

### Tests

| Test | Assertion |
|---|---|
| `test_discover_returns_config_file_list` | Returns `list[ConfigFile]` |
| `test_discover_skips_ai_when_no_endpoint` | No endpoint -> AI not called |
| `test_discover_never_returns_reason_unknown` | No `reason="unknown"` or `"ambiguous"` in output |
| `test_discover_excludes_ssh_private_keys_end_to_end` | SSH keys never in output |
| `test_discover_ai_only_receives_unknowns` | Only `include=None` files sent to AI |

---

## Acceptance criteria

- [ ] `discover()` returns `list[ConfigFile]` — no `None` verdicts remain
- [ ] SSH private keys (`.ssh/id_*`) never appear in output
- [ ] AI triage is only invoked for ambiguous files
- [ ] Classification cache persists across runs
- [ ] All tests pass: `uv run pytest tests/test_discovery.py tests/test_llm_client.py -v`
