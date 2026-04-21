"""CLI entry point for DotSync."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import typer
from rich.live import Live
from typing_extensions import Annotated

from dotsync.llm_client import probe_llm
from dotsync.logging_setup import setup_logging

if TYPE_CHECKING:
    from pathlib import Path

    from dotsync.config import DotSyncConfig
    from dotsync.discovery import ConfigFile
    from dotsync.flagging import FlagResult
    from dotsync.git_ops import ManifestEntry

app = typer.Typer(name="dotsync", help="Backup, sync, and encrypt dotfiles across workstations.")

logger = logging.getLogger("dotsync")

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------

EXIT_CODES = {
    "success": 0,
    "health_check_failed": 1,
    "dependency_missing": 2,
    "config_not_found": 3,
    "merge_conflict": 4,
    "user_aborted": 5,
}

# ---------------------------------------------------------------------------
# Shared state (set by callback)
# ---------------------------------------------------------------------------

_verbose = False


@app.callback()
def callback(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable verbose logging")] = False,
) -> None:
    """Initialize logging for all commands."""
    global _verbose
    _verbose = verbose
    setup_logging(verbose=verbose)


# ---------------------------------------------------------------------------
# Sensitive file confirmation (UI concern, lives in CLI layer)
# ---------------------------------------------------------------------------


def confirm_sensitive_files(flag_results: list[FlagResult]) -> list[FlagResult]:
    """Prompt user for each file flagged as sensitive.

    Updates ``FlagResult.requires_confirmation`` based on user response:
    - ``I`` (Include): set ``requires_confirmation = False``
    - ``E`` (Exclude): set ``config_file.include = False``, ``requires_confirmation = False``
    - ``S`` (Skip): leave as-is (still ``requires_confirmation = True``)

    Args:
        flag_results: FlagResult objects from ``flag_all()``.

    Returns:
        The same (mutated) list.
    """
    from dotsync.ui import console, flag_panel

    sensitive = [fr for fr in flag_results if fr.requires_confirmation]
    if not sensitive:
        return flag_results

    console.print(f"\n[yellow]![/yellow] {len(sensitive)} file(s) flagged as sensitive:\n")

    for fr in sensitive:
        console.print(flag_panel(fr))
        choice = typer.prompt(
            "  [I]nclude / [E]xclude / [S]kip",
            default="S",
        ).strip().upper()

        if choice == "I":
            fr.requires_confirmation = False
        elif choice == "E":
            fr.config_file.include = False
            fr.requires_confirmation = False
        # "S" or anything else → leave as-is

    return flag_results


def _mark_sensitive(flag_results: list[FlagResult]) -> None:
    """Mark ConfigFiles as sensitive based on flagging results.

    After ``confirm_sensitive_files()`` resolves interactive prompts, any
    file that had actual detections (regex matches or AI-flagged) and was
    confirmed for inclusion (``requires_confirmation is False``) is marked
    ``sensitive=True``.

    Args:
        flag_results: FlagResult objects (already confirmed).
    """
    for fr in flag_results:
        if (fr.matches or fr.ai_flagged) and not fr.requires_confirmation:
            fr.config_file.sensitive = True


# ---------------------------------------------------------------------------
# LLM connectivity pre-check
# ---------------------------------------------------------------------------


def _check_llm_connectivity(cfg: DotSyncConfig) -> None:
    """Probe the LLM endpoint before AI triage begins.

    If the probe fails, asks the user whether to continue without AI triage.
    On confirmation, clears ``cfg.llm_endpoint`` so all subsequent AI calls
    are skipped. On refusal, exits with ``user_aborted``.

    Args:
        cfg: DotSync configuration (mutated in-place if user skips AI).
    """
    if cfg.llm_endpoint is None:
        return

    from dotsync.ui import console, print_success, print_warning

    with console.status("Testing LLM connectivity..."):
        reachable, reason = probe_llm(cfg.llm_endpoint, cfg.llm_model, api_key=cfg.llm_api_key)

    if reachable:
        print_success(f"LLM endpoint reachable: {cfg.llm_endpoint}")
        return

    detail = f" ({reason})" if reason else ""
    print_warning(
        f"LLM endpoint check failed: {cfg.llm_endpoint}{detail}\n"
        "  AI triage will be skipped if you continue."
    )
    if not typer.confirm("Continue without AI triage?", default=True):
        raise typer.Exit(code=EXIT_CODES["user_aborted"])

    cfg.llm_endpoint = None


# ---------------------------------------------------------------------------
# Live scan progress
# ---------------------------------------------------------------------------


def _run_discover_with_progress(cfg: DotSyncConfig) -> list[ConfigFile]:
    """Run discover() with a live progress display.

    Args:
        cfg: DotSyncConfig instance.

    Returns:
        List of ConfigFile results from discover().
    """
    import time

    from dotsync.discovery import ScanEvent, discover as run_discover
    from dotsync.ui import ScanStats, console, make_scan_display

    stats = ScanStats(start_time=time.monotonic())
    accepted_paths: list[str] = []
    ai_errors: list[str] = []

    def on_event(event: ScanEvent) -> None:
        t = event["type"]
        if t == "dir_enter":
            stats.dirs_entered += 1
            stats.current_dir = event["path"] or ""
        elif t == "dir_pruned":
            stats.dirs_pruned += 1
        elif t == "file_accepted":
            stats.files_accepted += 1
            accepted_paths.append(event["path"] or "")
        elif t == "file_rejected":
            stats.files_rejected += 1
        elif t == "phase_start":
            stats.phase = event["reason"] or ""
        elif t == "phase_done" and event["reason"] == "scan":
            # After scan phase, log the full list of accepted files
            for p in accepted_paths:
                logger.debug("accepted: %s", p)
        elif t == "ai_batch":
            stats.ai_batches_done += 1
            if event.get("total"):
                stats.ai_batches_total = event["total"]
        elif t == "ai_error":
            ai_errors.append(event["reason"] or "unknown error")
        # Log pruned/rejected at DEBUG for --verbose visibility
        if t in ("dir_pruned", "file_rejected"):
            logger.debug("%s: %s (%s)", t, event["path"], event["reason"])
        live.update(make_scan_display(stats))

    with Live(make_scan_display(stats), console=console, refresh_per_second=8) as live:
        files = run_discover(cfg, progress=on_event)

    if ai_errors:
        from dotsync.ui import print_warning
        print_warning(
            f"AI classification failed: {ai_errors[0]}. "
            "Ambiguous files will require manual review. "
            "Check that the LLM endpoint is running and reachable."
        )

    return files


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def init(
    repo_path: Annotated[str | None, typer.Option("--repo-path", help="Path for the dotfiles repository")] = None,
    remote: Annotated[str | None, typer.Option("--remote", help="Git remote URL")] = None,
    llm_endpoint: Annotated[str | None, typer.Option("--llm-endpoint", help="LiteLLM proxy endpoint")] = None,
) -> None:
    """Initialize DotSync configuration and repository."""
    from pathlib import Path

    from dotsync.config import CONFIG_FILE, default_config, save_config
    from dotsync.git_ops import MissingDependencyError, check_dependencies, init_gitcrypt, init_repo, set_remote
    from dotsync.ui import console, print_error, print_success, print_warning

    try:
        check_dependencies()
    except MissingDependencyError as exc:
        print_error(str(exc))
        raise typer.Exit(code=EXIT_CODES["dependency_missing"]) from exc

    if CONFIG_FILE.exists():
        typer.echo(f"Configuration file already exists at {CONFIG_FILE}")
        if not typer.confirm("Do you want to overwrite it with default settings?"):
            typer.echo("Initialization cancelled.")
            raise typer.Exit()

    cfg = default_config()

    if repo_path:
        cfg.repo_path = Path(repo_path)
    if llm_endpoint:
        cfg.llm_endpoint = llm_endpoint

    with console.status("Initializing repository..."):
        repo = init_repo(cfg)

    print_success(f"Repository initialized at {cfg.repo_path}")

    # git-crypt setup
    key_path = cfg.repo_path / "dotsync.key"
    try:
        with console.status("Setting up git-crypt..."):
            init_gitcrypt(cfg.repo_path, key_path)
        cfg.gitcrypt_key_path = key_path
        print_success("git-crypt initialized")
        print_warning(
            f"Key exported to {key_path} — back it up securely and never commit it!"
        )
    except Exception:
        print_warning("git-crypt init skipped (may already be initialized)")
        logger.debug("git-crypt init error", exc_info=True)

    if remote:
        set_remote(repo, remote)
        cfg.remote_url = remote
        print_success(f"Remote set to {remote}")

    save_config(cfg)
    print_success(f"Configuration saved to {CONFIG_FILE}")


@app.command()
def discover(
    no_ai: Annotated[bool, typer.Option("--no-ai", help="Skip AI classification")] = False,
) -> None:
    """Discover, flag, and register configuration files into the manifest."""
    from dotsync.config import ConfigNotFoundError, load_config
    from dotsync.flagging import enforce_never_include, flag_all
    from dotsync.git_ops import init_repo, load_manifest
    from dotsync.platform_utils import home_dir
    from dotsync.sync import register_new_files
    from dotsync.ui import console, file_table, print_error, print_section, print_success, print_warning

    try:
        cfg = load_config()
    except ConfigNotFoundError as exc:
        print_error(f"{exc} — run 'dotsync init' first.")
        raise typer.Exit(code=EXIT_CODES["config_not_found"]) from exc

    if no_ai:
        cfg.llm_endpoint = None

    _check_llm_connectivity(cfg)

    # 1. Scan & classify
    files = _run_discover_with_progress(cfg)

    print_section("Discovery Results")

    included = [f for f in files if f.include is True]
    excluded = [f for f in files if f.include is False]
    pending = [f for f in files if f.include is None]

    if included:
        console.print(f"\n[green]Included ({len(included)}):[/green]")
        console.print(file_table(included))

    if excluded:
        console.print(f"\n[red]Excluded ({len(excluded)}):[/red]")
        console.print(file_table(excluded))

    if pending:
        console.print(f"\n[yellow]Pending / ask_user ({len(pending)}):[/yellow]")
        console.print(file_table(pending))

        for f in pending:
            if typer.confirm(f"  Include {f.path}?"):
                f.include = True
                f.reason = "user_confirmed"
            else:
                f.include = False
                f.reason = "user_excluded"

    # 2. Enforce never-include blocklist
    enforce_never_include(files)

    # 3. Flag sensitive data
    included_files = [f for f in files if f.include is True]
    with console.status("Checking for sensitive data..."):
        flag_results = flag_all(included_files, cfg)

    # 4. Interactive confirmation for flagged files
    confirm_sensitive_files(flag_results)
    _mark_sensitive(flag_results)

    # 5. Load manifest to identify already-tracked files
    manifest = load_manifest(cfg.repo_path)
    tracked_paths = {e.relative_path for e in manifest}
    to_register = [
        f for f in files
        if f.include is True and str(f.path) not in tracked_paths
    ]
    already_tracked = [
        f for f in files
        if f.include is True and str(f.path) in tracked_paths
    ]
    excluded_final = [f for f in files if f.include is not True]

    # 6. Enhanced summary
    print_section("Summary")
    console.print(
        f"  [green]{len(to_register)}[/green] file(s) to register, "
        f"[dim]{len(already_tracked)}[/dim] already tracked, "
        f"[red]{len(excluded_final)}[/red] excluded"
    )

    # 7. Register new files
    if not to_register:
        print_success("Nothing new to register")
        return

    if not typer.confirm(f"Register {len(to_register)} new file(s) into manifest?"):
        print_warning("Registration cancelled")
        raise typer.Exit(code=EXIT_CODES["user_aborted"])

    home = home_dir()
    init_repo(cfg)
    new_entries = register_new_files(to_register, flag_results, cfg.repo_path, home, cfg)

    print_success(f"Registered {len(new_entries)} file(s) into manifest")


def _manifest_to_config_files(entries: list[ManifestEntry], home: Path) -> list[ConfigFile]:
    """Convert ManifestEntry objects to ConfigFile objects for flagging.

    Args:
        entries: ManifestEntry objects from the manifest.
        home: Home directory path.

    Returns:
        List of ConfigFile objects.
    """
    from pathlib import Path

    from dotsync.discovery import ConfigFile

    result: list[ConfigFile] = []
    for e in entries:
        abs_path = home / e.relative_path
        size = abs_path.stat().st_size if abs_path.exists() else 0
        result.append(
            ConfigFile(
                path=Path(e.relative_path),
                abs_path=abs_path,
                size_bytes=size,
                include=True,
                sensitive=e.sensitive_flagged,
                reason="manifest",
                os_profile=e.os_profile,
            )
        )
    return result


@app.command()
def sync(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would happen without making changes")] = False,
    no_push: Annotated[bool, typer.Option("--no-push", help="Commit but do not push")] = False,
    message: Annotated[str, typer.Option("--message", "-m", help="Custom commit message")] = "dotsync: sync",
) -> None:
    """Sync manifest files to the repository."""
    from dotsync.config import ConfigNotFoundError, load_config
    from dotsync.flagging import flag_all
    from dotsync.git_ops import (
        NoRemoteConfiguredError,
        commit_and_push,
        init_repo,
        load_manifest,
    )
    from dotsync.health import HealthCheckFailedError, post_operation_checks
    from dotsync.platform_utils import current_os, home_dir
    from dotsync.snapshot import create_snapshot
    from dotsync.sync import execute_sync, plan_sync
    from dotsync.ui import action_table, console, print_error, print_section, print_success, print_warning

    try:
        cfg = load_config()
    except ConfigNotFoundError as exc:
        print_error(f"{exc} — run 'dotsync init' first.")
        raise typer.Exit(code=EXIT_CODES["config_not_found"]) from exc

    home = home_dir()
    os_name = current_os()

    # 1. Load manifest — must have files already registered via discover
    manifest = load_manifest(cfg.repo_path)
    if not manifest:
        print_warning("Manifest is empty — run 'dotsync discover' first to register files.")
        raise typer.Exit(code=EXIT_CODES["user_aborted"])

    # 2. Flag manifest entries for sensitive data
    config_files = _manifest_to_config_files(manifest, home)
    with console.status("Checking for sensitive data..."):
        flag_results = flag_all(config_files, cfg)

    confirm_sensitive_files(flag_results)
    _mark_sensitive(flag_results)

    # 3. Snapshot before sync
    repo = init_repo(cfg)

    print_section("Snapshot")
    with console.status("Creating pre-sync snapshot..."):
        snap = create_snapshot(manifest, home, trigger="sync", keep=cfg.snapshot_keep)
    print_success(f"Snapshot created: {snap.id} ({snap.file_count} files)")

    # 4. Plan sync
    print_section("Sync")
    actions = plan_sync(manifest, home, cfg.repo_path, os_name)

    console.print(action_table(actions))

    copied = [a for a in actions if a.action == "copy"]
    skipped = [a for a in actions if a.action != "copy"]

    if dry_run:
        print_warning(f"Dry run: would sync {len(copied)} file(s), skip {len(skipped)}")
        return

    if not copied:
        print_success("Nothing to sync")
        return

    if not typer.confirm(f"Proceed with syncing {len(copied)} file(s)?"):
        print_warning("Sync cancelled")
        raise typer.Exit(code=EXIT_CODES["user_aborted"])

    # 5. Execute sync
    executed = execute_sync(actions, dry_run=False)
    copied_exec = [a for a in executed if a.action == "copy"]
    skipped_exec = [a for a in executed if a.action != "copy"]
    print_success(f"Synced {len(copied_exec)} file(s), skipped {len(skipped_exec)}")

    # 6. Commit & push
    try:
        if no_push:
            repo.git.add(A=True)
            if repo.is_dirty(index=True) or repo.untracked_files:
                repo.index.commit(message)
                print_success("Changes committed (push skipped)")
        else:
            commit_and_push(repo, message)
            print_success("Changes committed and pushed")
    except NoRemoteConfiguredError:
        repo.git.add(A=True)
        if repo.is_dirty(index=True) or repo.untracked_files:
            repo.index.commit(message)
        print_warning("No remote configured — committed locally only")
    except Exception as exc:
        # Push failed (e.g. rejected, auth error) — commit is already local
        print_warning(f"Changes committed locally but push failed: {exc}")

    # 7. Health checks
    print_section("Health Checks")
    try:
        with console.status("Running health checks..."):
            post_operation_checks(cfg, snap.id, home, operation="sync")
        print_success("All health checks passed")
    except HealthCheckFailedError as exc:
        print_error(str(exc))
        raise typer.Exit(code=EXIT_CODES["health_check_failed"]) from exc


@app.command()
def restore(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would happen without making changes")] = False,
    no_pull: Annotated[bool, typer.Option("--no-pull", help="Do not pull before restoring")] = False,
    from_snapshot: Annotated[str | None, typer.Option("--from-snapshot", help="Restore from a specific snapshot ID")] = None,
) -> None:
    """Restore configuration files from the repository."""
    from dotsync.config import ConfigNotFoundError, load_config
    from dotsync.git_ops import (
        MergeConflictError,
        NoRemoteConfiguredError,
        init_repo,
        load_manifest,
        pull,
    )
    from dotsync.health import HealthCheckFailedError, post_operation_checks
    from dotsync.platform_utils import current_os, home_dir
    from dotsync.snapshot import create_snapshot, rollback as snapshot_rollback
    from dotsync.sync import execute_restore, plan_restore
    from dotsync.ui import console, print_error, print_section, print_success, print_warning

    try:
        cfg = load_config()
    except ConfigNotFoundError as exc:
        print_error(f"{exc} — run 'dotsync init' first.")
        raise typer.Exit(code=EXIT_CODES["config_not_found"]) from exc

    home = home_dir()
    os_name = current_os()

    # Shortcut: restore from snapshot directly
    if from_snapshot:
        print_section("Rollback from Snapshot")
        restored = snapshot_rollback(from_snapshot, home, dry_run=dry_run)
        if dry_run:
            print_warning(f"Dry run: would restore {len(restored)} file(s) from snapshot {from_snapshot}")
        else:
            print_success(f"Restored {len(restored)} file(s) from snapshot {from_snapshot}")
        return

    repo = init_repo(cfg)

    # 1. Pull
    if not no_pull:
        try:
            with console.status("Pulling from remote..."):
                pull(repo)
            print_success("Pulled latest changes")
        except NoRemoteConfiguredError:
            print_warning("No remote configured — skipping pull")
        except MergeConflictError as exc:
            print_error(f"{exc}")
            raise typer.Exit(code=EXIT_CODES["merge_conflict"]) from exc

    # 2. Load manifest & snapshot
    manifest = load_manifest(cfg.repo_path)

    print_section("Snapshot")
    with console.status("Creating pre-restore snapshot..."):
        snap = create_snapshot(manifest, home, trigger="restore", keep=cfg.snapshot_keep)
    print_success(f"Snapshot created: {snap.id} ({snap.file_count} files)")

    # 3. Plan & execute restore
    print_section("Restore")
    actions = plan_restore(manifest, home, cfg.repo_path, os_name)
    executed = execute_restore(actions, dry_run=dry_run)

    restored_actions = [a for a in executed if a.action == "restore"]
    skipped_actions = [a for a in executed if a.action != "restore"]

    if dry_run:
        print_warning(f"Dry run: would restore {len(restored_actions)} file(s), skip {len(skipped_actions)}")
    else:
        print_success(f"Restored {len(restored_actions)} file(s), skipped {len(skipped_actions)}")

    # 4. Health checks
    if not dry_run:
        print_section("Health Checks")
        try:
            with console.status("Running health checks..."):
                post_operation_checks(cfg, snap.id, home, operation="restore")
            print_success("All health checks passed")
        except HealthCheckFailedError as exc:
            print_error(str(exc))
            raise typer.Exit(code=EXIT_CODES["health_check_failed"]) from exc


@app.command()
def rollback(
    snapshot_id: Annotated[str | None, typer.Argument(help="Snapshot ID to rollback to")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would happen without making changes")] = False,
    list_all: Annotated[bool, typer.Option("--list", help="List available snapshots and exit")] = False,
) -> None:
    """Rollback to a previous snapshot."""
    from dotsync.git_ops import load_manifest
    from dotsync.platform_utils import home_dir
    from dotsync.snapshot import (
        SnapshotNotFoundError,
        list_snapshots,
        rollback as snapshot_rollback,
        verify_snapshot,
    )
    from dotsync.ui import console, print_error, print_section, print_success, print_warning, snapshot_table

    snapshots = list_snapshots()

    if list_all:
        print_section("Available Snapshots")
        if not snapshots:
            print_warning("No snapshots available")
        else:
            console.print(snapshot_table(snapshots))
        return

    if not snapshots:
        print_error("No snapshots available")
        raise typer.Exit(code=1)

    # If no ID provided, interactive selection
    if snapshot_id is None:
        print_section("Available Snapshots")
        console.print(snapshot_table(snapshots))
        choice = typer.prompt(
            "\nSelect snapshot number",
            type=int,
            default=1,
        )
        if choice < 1 or choice > len(snapshots):
            print_error(f"Invalid selection: {choice}")
            raise typer.Exit(code=1)
        snapshot_id = snapshots[choice - 1].id

    # Verify snapshot integrity
    try:
        from dotsync.config import load_config

        cfg = load_config()
        manifest = load_manifest(cfg.repo_path)
        result = verify_snapshot(snapshot_id, manifest)
        if not result["complete"]:
            missing_files = result.get("missing", [])
            n_missing = len(missing_files) if isinstance(missing_files, list) else 0
            print_warning(f"Snapshot {snapshot_id} is incomplete ({n_missing} file(s) missing)")
            if not typer.confirm("Continue anyway?"):
                raise typer.Exit(code=EXIT_CODES["user_aborted"])
    except Exception:
        logger.debug("Snapshot verification skipped", exc_info=True)

    home = home_dir()
    try:
        restored = snapshot_rollback(snapshot_id, home, dry_run=dry_run)
    except SnapshotNotFoundError as exc:
        print_error(str(exc))
        raise typer.Exit(code=1) from exc

    if dry_run:
        print_warning(f"Dry run: would restore {len(restored)} file(s) from {snapshot_id}")
    else:
        print_success(f"Rolled back {len(restored)} file(s) from {snapshot_id}")


@app.command()
def status() -> None:
    """Show current DotSync status."""
    from dotsync.config import CONFIG_FILE, ConfigNotFoundError, load_config
    from dotsync.git_ops import get_remote, init_repo, load_manifest
    from dotsync.snapshot import list_snapshots
    from dotsync.ui import console, print_error, print_section

    try:
        cfg = load_config()
    except ConfigNotFoundError as exc:
        print_error(f"{exc} — run 'dotsync init' first.")
        raise typer.Exit(code=EXIT_CODES["config_not_found"]) from exc

    print_section("DotSync Status")

    from rich.table import Table

    table = Table(show_header=False, box=None)
    table.add_column("Key", style="bold")
    table.add_column("Value")

    table.add_row("Config file", str(CONFIG_FILE))
    table.add_row("Repo path", str(cfg.repo_path))
    table.add_row("Remote URL", cfg.remote_url or "(not set)")
    table.add_row("LLM endpoint", cfg.llm_endpoint or "(not set)")
    table.add_row("LLM API key", "***" if cfg.llm_api_key else "(not set)")
    table.add_row("LLM model", cfg.llm_model)
    table.add_row("Snapshot retention", str(cfg.snapshot_keep))

    repo = init_repo(cfg)
    remote = get_remote(repo)
    table.add_row("Git remote", remote or "(none)")

    manifest = load_manifest(cfg.repo_path)
    table.add_row("Managed files", str(len(manifest)))

    snapshots = list_snapshots()
    table.add_row("Snapshots", str(len(snapshots)))

    if cfg.health_checks:
        table.add_row("Health checks", ", ".join(cfg.health_checks))
    else:
        table.add_row("Health checks", "(defaults only)")

    console.print(table)


@app.command()
def config(
    show: Annotated[bool, typer.Option("--show", help="Show current configuration")] = False,
    set_value: Annotated[str | None, typer.Option("--set", help="Set a config value (KEY=VALUE)")] = None,
) -> None:
    """View or modify DotSync configuration."""
    from pathlib import Path

    from dotsync.config import ConfigNotFoundError, DotSyncConfig, load_config, save_config
    from dotsync.ui import console, print_error, print_success

    try:
        cfg = load_config()
    except ConfigNotFoundError as exc:
        print_error(f"{exc} — run 'dotsync init' first.")
        raise typer.Exit(code=EXIT_CODES["config_not_found"]) from exc

    if show:
        from rich.table import Table

        table = Table(title="Configuration")
        table.add_column("Key", style="cyan")
        table.add_column("Value")

        for field_name, field_info in DotSyncConfig.model_fields.items():
            value = getattr(cfg, field_name)
            table.add_row(field_name, str(value) if value is not None else "(not set)")

        console.print(table)
        return

    if set_value:
        if "=" not in set_value:
            print_error("Invalid format. Use --set KEY=VALUE")
            raise typer.Exit(code=1)

        key, _, value = set_value.partition("=")
        key = key.strip()
        value = value.strip()

        valid_keys = set(DotSyncConfig.model_fields.keys())
        if key not in valid_keys:
            print_error(f"Unknown config key: '{key}'. Valid keys: {', '.join(sorted(valid_keys))}")
            raise typer.Exit(code=1)

        field_type = DotSyncConfig.model_fields[key].annotation

        # Coerce value to the right type
        if field_type is int:
            setattr(cfg, key, int(value))
        elif field_type is Path:
            setattr(cfg, key, Path(value))
        elif "list" in str(field_type).lower():
            # For list fields, split as comma-separated and coerce element type
            parts = [v.strip() for v in value.split(",") if v.strip()]
            coerced: list[str] | list[Path] = [Path(v) for v in parts] if "Path" in str(field_type) else parts
            setattr(cfg, key, coerced)
        else:
            # str or Optional[str]
            setattr(cfg, key, value if value else None)

        save_config(cfg)
        print_success(f"Set {key} = {value}")
        return

    # No flags — show help
    typer.echo("Use --show to display config or --set KEY=VALUE to update a value.")


if __name__ == "__main__":
    app()
