"""CLI entry point for DotSync."""

import typer
from typing_extensions import Annotated

from dotsync.logging_setup import setup_logging

app = typer.Typer(name="dotsync", help="Backup, sync, and encrypt dotfiles across workstations.")


@app.callback()
def callback(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable verbose logging")] = False,
) -> None:
    """Initialize logging for all commands."""
    setup_logging(verbose=verbose)


@app.command()
def init() -> None:
    """Initialize DotSync configuration and repository."""
    from dotsync.config import CONFIG_FILE, default_config, save_config

    if CONFIG_FILE.exists():
        typer.echo(f"Configuration file already exists at {CONFIG_FILE}")
        if not typer.confirm("Do you want to overwrite it with default settings?"):
            typer.echo("Initialization cancelled.")
            raise typer.Exit()

    cfg = default_config()
    save_config(cfg)
    typer.echo(f"Configuration created at {CONFIG_FILE}")


@app.command()
def sync() -> None:
    """Sync configuration files with the repository."""
    typer.echo("Command 'sync' not implemented yet.")


@app.command()
def restore() -> None:
    """Restore configuration files from the repository."""
    typer.echo("Command 'restore' not implemented yet.")


@app.command()
def rollback() -> None:
    """Rollback to a previous snapshot."""
    typer.echo("Command 'rollback' not implemented yet.")


@app.command()
def status() -> None:
    """Show current status of the repository."""
    typer.echo("Command 'status' not implemented yet.")


if __name__ == "__main__":
    app()
