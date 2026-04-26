# Init Command Specification

## Purpose

Initialize the DotSync configuration file with default settings.

## Behavior

### Fresh Installation

When `~/.dotsync/config.toml` does not exist:
1. Create the `~/.dotsync/` directory if needed
2. Generate default configuration
3. Save to `~/.dotsync/config.toml`
4. Display success message with file path

### Existing Configuration

When `~/.dotsync/config.toml` already exists:
1. Display warning that file exists
2. Prompt user for confirmation: "Do you want to overwrite it with default settings?"
3. If user declines:
   - Display "Initialization cancelled."
   - Exit without modifying the file
4. If user confirms:
   - Overwrite with default configuration
   - Display success message

## Default Configuration

```toml
repo_path = "~/dotsync-repo"
llm_model = "claude-haiku-4-5"
snapshot_keep = 5
health_checks = []
exclude_patterns = []
include_extra = []
```

Optional fields (omitted if not set):
- `remote_url`
- `llm_endpoint`

## Implementation Details

- Uses `typer.confirm()` for yes/no prompt
- Default response is "No" (safe default)
- Exits with code 1 if cancelled
- Uses `DotSyncConfig` dataclass for type safety
