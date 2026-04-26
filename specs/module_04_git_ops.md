# Module 04 — Git Operations

## Responsibility

Manage the dotfiles Git repository. Wraps GitPython for standard Git operations. This is the storage backbone — all synced config files are committed to this repo.

---

## Step 4.1 — Dependency checks

### `check_dependencies() -> None`

- Use `shutil.which("git")`
- Raise `MissingDependencyError` with platform-appropriate install hints
- Use `platform_utils.current_os()` to decide linux vs windows hint text

### Exceptions

```python
class MissingDependencyError(Exception): ...
class NoRemoteConfiguredError(Exception): ...
class MergeConflictError(Exception): ...
```

---

## Step 4.2 — Repository initialization

### `init_repo(cfg: DotSyncConfig) -> git.Repo`

- `cfg.repo_path.mkdir(parents=True, exist_ok=True)`
- If `.git` already exists → open and return existing `Repo` (idempotent)
- Otherwise `git.Repo.init(cfg.repo_path)`
- Write `.gitattributes` with exclusions for `.gitattributes` and `.dotsync_manifest.json`
- Write empty `.dotsync_manifest.json` (`[]`)
- Stage both files, initial commit `"chore: init dotsync repo"`

### `.gitattributes` content

```
.gitattributes !filter !diff
.dotsync_manifest.json !filter !diff
```

---

## Step 4.3 — Remote management

### `set_remote(repo: git.Repo, remote_url: str) -> None`

- If "origin" exists → update URL via `remote.set_url()`
- Otherwise → `repo.create_remote("origin", url)`

### `get_remote(repo: git.Repo) -> str | None`

- Return `repo.remotes.origin.url` or `None` if no origin

---

## Step 4.4 — Manifest management

### `ManifestEntry` dataclass

```python
@dataclass
class ManifestEntry:
    relative_path: str
    os_profile: str
    added_at: str
    sensitive_flagged: bool
```

### Functions

| Function | Behavior |
|---|---|
| `load_manifest(repo_path)` | Read `.dotsync_manifest.json`, return `[]` on missing/corrupt |
| `save_manifest(repo_path, entries)` | Write JSON with indent=2 |
| `add_to_manifest(repo_path, entry)` | Load, deduplicate by `relative_path`, append if new, save |
| `remove_from_manifest(repo_path, relative_path)` | Load, filter, save |

---

## Step 4.5 — Commit, push, pull

### `commit_and_push(repo: git.Repo, message: str) -> None`

- `repo.git.add(A=True)` to stage all
- Skip commit if nothing dirty and no untracked files
- `repo.index.commit(message)`
- If no remote → raise `NoRemoteConfiguredError`
- `repo.remotes.origin.push()`

### `pull(repo: git.Repo) -> None`

- If no remote → raise `NoRemoteConfiguredError`
- `repo.remotes.origin.pull()`
- Check `repo.index.unmerged_blobs()` — if non-empty → raise `MergeConflictError`

---

## Step 4.6 — File copying

### `copy_to_repo(source: Path, home: Path, repo_path: Path) -> Path`

- `rel = source.relative_to(home)`
- `dest = repo_path / rel`
- `dest.parent.mkdir(parents=True, exist_ok=True)`
- `shutil.copy2(source, dest)` — preserves metadata
- Return `dest`

---

## Acceptance criteria

- [ ] `check_dependencies` raises with platform-specific hints when git is missing
- [ ] `init_repo` creates `.git`, `.gitattributes`, `.dotsync_manifest.json` with initial commit
- [ ] `init_repo` is idempotent — returns existing repo on second call
- [ ] `.gitattributes` and `.dotsync_manifest.json` excluded from diff filtering
- [ ] `set_remote` creates/updates origin, `get_remote` returns URL or None
- [ ] Manifest round-trips through save/load, deduplicates on add, filters on remove
- [ ] `commit_and_push` stages all, commits, pushes; skips when clean
- [ ] `pull` detects merge conflicts via `unmerged_blobs()`
- [ ] `copy_to_repo` preserves relative paths, creates parents, overwrites existing
