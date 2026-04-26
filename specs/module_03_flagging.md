# Module 03 — Sensitive Data Flagging

## Responsibility

Scan files marked `include=True` by the discovery module for credentials, API keys, PEM blocks, and other sensitive data before they enter the git repository. Users should consciously decide about files containing secrets.

---

## Step 3.1 — Sensitive patterns and constants

### `SENSITIVE_PATTERNS` (dict of compiled regexes)

11 patterns detecting common secret formats:

| Key | Pattern | Notes |
|---|---|---|
| `github_token` | `ghp_[A-Za-z0-9]{36}` | Classic PAT |
| `github_fine` | `github_pat_[A-Za-z0-9_]{22,}` | Fine-grained PAT |
| `aws_access_key` | `AKIA[0-9A-Z]{16}` | AWS access key ID |
| `aws_secret` | `aws_secret_access_key\s*[=:]\s*\S+` | Case-insensitive |
| `openai_key` | `sk-[A-Za-z0-9]{20,}` | OpenAI API key |
| `anthropic_key` | `sk-ant-[A-Za-z0-9-]{20,}` | Anthropic API key |
| `generic_api_key` | `api[_-]?key\s*[=:]\s*\S+` | Case-insensitive; skips `#` comments |
| `generic_token` | `token\s*[=:]\s*\S+` | Case-insensitive; skips `#` comments |
| `email` | `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z\|a-z]{2,}` | Email address |
| `private_key_pem` | `-----BEGIN (RSA )?PRIVATE KEY-----` | PEM private key header |
| `connection_string` | `(mongodb\|postgres\|mysql\|redis\|amqp)://\S+` | Database/broker URLs |

### `NEVER_INCLUDE` (list)

Defense-in-depth blocklist — these files are never included even if discovery said `include=True`:

```
.ssh/id_rsa
.ssh/id_ed25519
.ssh/id_ecdsa
.gnupg/          (directory prefix match)
```

Note: `SAFETY_EXCLUDES` in discovery already blocks these at scan time; this is a second layer.

---

## Step 3.2 — File scanning

### `scan_file_for_secrets(path: Path) -> list[SensitiveMatch]`

- Read file line by line as UTF-8
- For each line, check all `SENSITIVE_PATTERNS`
- Skip `#`-prefixed lines only for `generic_token` and `generic_api_key`
- Redact matched values: show first 2 and last 2 chars with `***` in between (≤4 chars → `***` only)
- Return `[]` on `UnicodeDecodeError` or `OSError`

### `SensitiveMatch` dataclass

```python
@dataclass
class SensitiveMatch:
    pattern_name: str
    line_number: int
    preview: str  # redacted
```

---

## Step 3.3 — AI flag check

### `ai_flag_check(path: Path, cfg: DotSyncConfig) -> bool`

- If `cfg.llm_endpoint` is `None` → return `False`
- Read first 30 lines of file
- Build JSON payload: `{"path": str, "size_bytes": int, "first_lines": list[str]}`
- Call `chat_completion()` from `llm_client` (no direct httpx)
- Parse response for `{"sensitive": bool, "reason": str}`
- Cache in `~/.dotsync/sensitivity_cache.json` keyed by `"{path}:{mtime}"`
- On `LLMError` / `json.JSONDecodeError` → return `False` (fail open)

---

## Step 3.4 — Orchestration

### `flag_all(files: list[ConfigFile], cfg: DotSyncConfig) -> list[FlagResult]`

- Filter to `include=True` files only
- Run `scan_file_for_secrets` on each
- Run `ai_flag_check` only if no regex matches found (avoid redundant AI calls)
- Set `requires_confirmation = bool(matches) or ai_flagged`

> **Note:** `flag_all()` is called only during `sync`, not during `discover`. Discovery
> focuses on classification and registration; sensitive data verification happens when
> files are about to be committed to the repository.

### `FlagResult` dataclass

```python
@dataclass
class FlagResult:
    config_file: ConfigFile
    matches: list[SensitiveMatch]
    ai_flagged: bool
    requires_confirmation: bool
```

---

## Step 3.5 — Never-include enforcement

### `enforce_never_include(files: list[ConfigFile]) -> list[ConfigFile]`

- Match relative path against `NEVER_INCLUDE`
- Trailing-slash entries match as directory prefix
- Non-slash entries require exact match
- Matched: set `include=False`, `reason="never_include"`
- Returns the mutated list

---

## Acceptance criteria

- [ ] 11 regex patterns compile and match expected inputs
- [ ] `scan_file_for_secrets` returns matches with correct line numbers and redacted previews
- [ ] Comment lines skipped for generic patterns only
- [ ] Binary/unreadable files handled gracefully
- [ ] AI check caches by `path:mtime`, invalidates on file change
- [ ] AI errors fail open (return `False`)
- [ ] `flag_all` skips `include!=True` files
- [ ] AI not called when regex already matched
- [ ] `enforce_never_include` forces `include=False` for blocklisted paths
- [ ] 21 tests passing, ruff clean, mypy clean
