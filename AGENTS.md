# CLAUDE.md

## Project

**DotSync** — CLI tool to backup, sync, and restore configuration files (dotfiles) across Windows and Linux workstations. Backing store is a Git repo. Optional AI triage via LiteLLM proxy.

Key architectural decisions (do not change without instruction):
- `GitPython` handles all Git operations
- AI calls use plain `httpx` targeting OpenAI-compatible endpoints (`/v1/chat/completions`) — no LLM SDK. Works with LiteLLM, OpenRouter, Ollama, or any OpenAI-compatible proxy.
- Snapshots are local only, never committed to the repo

---

## Runtime & Tooling

- **Python 3.12.x** — mandatory
- **uv only** — no pip, poetry, pipenv, or conda
- `uv.lock` must be committed
- Unix-like shell assumed unless stated otherwise

---

## Project Structure

```
dotsync/
├── pyproject.toml
├── uv.lock
├── src/
│   └── dotsync/
│       ├── main.py        # Typer CLI entry point
│       ├── config.py
│       ├── discovery.py
│       ├── flagging.py
│       ├── git_ops.py
│       ├── sync.py
│       ├── snapshot.py
│       ├── health.py
│       └── ui.py
└── tests/                 # mirrors src/dotsync/
```

- Business logic lives under `src/` only
- Tests mirror `src/dotsync/` structure

---

## Common Commands

```bash
uv sync                          # install deps
uv run dotsync                   # run CLI
uv run pytest                    # all tests
uv run pytest --cov=dotsync --cov-report=term-missing
uv run ruff check . && uv run ruff format .
uv run mypy src/
```

CI must pass: `uv sync --frozen && ruff check && mypy src/ && pytest`

---

## Coding Standards

- `pathlib` over `os.path`
- `dataclasses` or `pydantic` for structured data
- Modern typing: `list[str]`, `dict[str, int]`, `Literal[...]`
- All public functions: type hints + concise docstring
- No mutable default arguments
- No side effects at import time
- No hardcoded paths or secrets

---

## Testing Rules

- All features require tests; bug fixes require regression tests
- No external network calls in unit tests — mock `httpx` and `subprocess`
- No external filesystem assumptions — use `tmp_path` fixture
- Tests must be deterministic

---

## Documentation

| File | Purpose |
|---|---|
| `specs/` | Module-by-module technical specs and incremental steps |
| `docs/architecture.md` | System design and data flow |
| `docs/changelog.md` | Version history |
| `docs/project_status.md` | Current progress and pending work |

After every major milestone or significant addition: update the relevant files in `docs/`.

---

## When Modifying Code

1. Read the full relevant module before editing
2. Preserve architecture unless explicitly instructed
3. Keep diffs minimal — avoid unnecessary refactoring
4. Update or add tests for every change
