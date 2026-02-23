# Module 02 — Discovery & Classification

## Responsibility

Scan known filesystem locations for candidate configuration files, apply rule-based filtering to produce an initial include/exclude verdict, and optionally pass ambiguous candidates to an AI agent for triage. Output is a list of `ConfigFile` objects ready for user review.

---

## Step 2.1 — Known file/directory allowlist (rule-based baseline)

### `KNOWN_FILES` (frozenset)

Files that are **always included** when found under `$HOME`:

```
.gitconfig
.gitignore_global
.bashrc
.bash_profile
.zshrc
.zshenv
.profile
.vimrc
.ideavimrc
.tmux.conf
.wslconfig
.ssh/config
```

### `KNOWN_DIRS` (frozenset)

Every file **under** these directories is included:

```
.config/fish
.config/nvim
.config/alacritty
.config/kitty
.config/starship.toml
.config/htop
AppData/Roaming/Code/User
.config/Code/User
```

### `HARDCODED_EXCLUDES` (frozenset)

Paths matching these patterns are **never** included:

```
.ssh/id_*
.gnupg/
.cache/
.local/share/
node_modules/
__pycache__/
.git/
```

Glob patterns (containing `*`) are matched with `fnmatch`. Trailing-slash patterns match as directory prefixes.

### Tests

| Test | Assertion |
|---|---|
| `test_known_files_are_valid_relative_paths` | No entry starts with `/` |
| `test_hardcoded_excludes_not_in_known_files` | No overlap between sets |

---

## Step 2.2 — Filesystem scanner

### `scan_candidates(extra_paths=None) -> list[Path]`

Walk each root returned by `config_dirs()`:

| Rule | Detail |
|---|---|
| No symlink following | `followlinks=False`; skip symlink dirs and files |
| Max depth | 4 levels below each root |
| Size gate | Skip files > 1 MB |
| Binary gate | Read first 8 KB; skip if `\x00` found |
| Hardcoded excludes | `fnmatch` against `HARDCODED_EXCLUDES` |
| Extra paths | Append `extra_paths` if they exist on disk |
| Dedup | By `Path.resolve()` |

Returns a deduplicated `list[Path]` of absolute paths.

### Tests

| Test | Assertion |
|---|---|
| `test_scan_excludes_ssh_private_keys` | `.ssh/id_rsa` filtered, `.ssh/config` kept |
| `test_scan_skips_large_files` | >1 MB file excluded |
| `test_scan_skips_binary_files` | File with null bytes excluded |
| `test_scan_respects_max_depth` | Depth > 4 excluded |
| `test_scan_includes_extra_paths` | Extra path appended |

---

## Step 2.3 — Rule-based classifier

### `classify_rule_based(candidates, exclude_patterns=None, include_extra=None) -> list[ConfigFile]`

For each candidate path:

1. If it matches a user `exclude_patterns` glob -> `include=False, reason="user_excluded"`
2. If it matches `include_extra` -> `include=True, reason="user_included"`
3. If relative path is in `KNOWN_FILES` -> `include=True, reason="known"`
4. If relative path starts with a `KNOWN_DIRS` entry -> `include=True, reason="known_dir"`
5. Otherwise -> `include=None, reason="unknown"`

`os_profile` detection:

- `"windows"` if path contains `AppData`
- `"linux"` if path starts with `.config` or contains `/home/`
- `"shared"` otherwise

### Tests

| Test | Assertion |
|---|---|
| `test_known_file_included` | `.bashrc` -> `include=True, reason="known"` |
| `test_user_excluded_pattern` | Pattern match -> `include=False` |
| `test_unknown_file_pending` | Unknown file -> `include=None` |
| `test_os_profile_windows` | `AppData/...` -> `os_profile="windows"` |

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

### `chat_completion(endpoint, model, system_prompt, user_message, *, timeout=15) -> str`

Send a chat-completion request to `{endpoint}/v1/chat/completions` and return the assistant content string.

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
| `test_chat_completion_raises_llm_error_on_missing_choices` | Malformed body -> `LLMError` |

---

## Step 2.6 — Discovery orchestrator

### `discover(cfg) -> list[ConfigFile]`

1. `scan_candidates(extra_paths=cfg.include_extra)`
2. `classify_rule_based(candidates, ...)`
3. Filter unknowns (`include is None`)
4. If `cfg.llm_endpoint` set, call `classify_with_ai(unknowns, cfg)`
5. Any remaining `include is None` -> `reason="ask_user"`
6. Return full list

### Tests

| Test | Assertion |
|---|---|
| `test_discover_returns_config_file_list` | Returns `list[ConfigFile]` |
| `test_discover_skips_ai_when_no_endpoint` | No endpoint -> AI not called |
| `test_discover_never_returns_reason_unknown` | No `reason="unknown"` in output |
| `test_discover_excludes_ssh_private_keys_end_to_end` | SSH keys never in output |
| `test_discover_ai_only_receives_unknowns` | Only `include=None` files sent to AI |

---

## Acceptance criteria

- [ ] `discover()` returns `list[ConfigFile]` — no `None` verdicts remain
- [ ] SSH private keys (`.ssh/id_*`) never appear in output
- [ ] AI triage is only invoked for ambiguous files
- [ ] Classification cache persists across runs
- [ ] All tests pass: `uv run pytest tests/test_discovery.py tests/test_llm_client.py -v`
