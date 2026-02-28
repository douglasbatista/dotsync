"""Rich terminal UI helpers for DotSync."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

if TYPE_CHECKING:
    from dotsync.discovery import ConfigFile
    from dotsync.flagging import FlagResult
    from dotsync.snapshot import SnapshotMeta

console = Console()
err_console = Console(stderr=True, style="bold red")


@dataclass
class ScanStats:
    """Accumulates scan event counts for live progress display."""

    dirs_entered: int = 0
    dirs_pruned: int = 0
    files_accepted: int = 0
    files_rejected: int = 0
    current_dir: str = ""
    phase: str = ""
    ai_batches_done: int = 0


def make_scan_display(stats: ScanStats) -> Table:
    """Build a compact Rich table showing live scan statistics.

    Args:
        stats: Current scan statistics.

    Returns:
        A Rich Table summarising the scan state.
    """
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("Key", style="bold cyan", no_wrap=True)
    table.add_column("Value")

    phase_label = stats.phase or "starting"
    table.add_row("Phase", phase_label)
    table.add_row("Dirs scanned", str(stats.dirs_entered))
    table.add_row("Dirs pruned", str(stats.dirs_pruned))
    table.add_row("Files accepted", str(stats.files_accepted))
    table.add_row("Files rejected", str(stats.files_rejected))

    if stats.ai_batches_done:
        table.add_row("AI batches", str(stats.ai_batches_done))

    if stats.current_dir:
        # Truncate long paths for display
        display_dir = stats.current_dir
        if len(display_dir) > 60:
            display_dir = "..." + display_dir[-57:]
        table.add_row("Current dir", f"[dim]{display_dir}[/dim]")

    return table


def print_success(msg: str) -> None:
    """Print a success message in green."""
    console.print(f"[green]v[/green] {msg}")


def print_warning(msg: str) -> None:
    """Print a warning message in yellow."""
    console.print(f"[yellow]![/yellow] {msg}")


def print_error(msg: str) -> None:
    """Print an error message to stderr in red."""
    err_console.print(f"[red]x[/red] {msg}")


def print_section(title: str) -> None:
    """Print a section heading rule."""
    console.rule(f"[bold]{title}[/bold]")


def file_table(files: list[ConfigFile]) -> Table:
    """Build a Rich table of discovered config files.

    Args:
        files: List of ConfigFile objects to display.

    Returns:
        A Rich Table ready to print.
    """
    table = Table(title="Discovered Files")
    table.add_column("Path", style="cyan")
    table.add_column("Size", justify="right")
    table.add_column("Verdict", style="bold")
    table.add_column("Reason")
    table.add_column("OS", style="dim")

    for f in files:
        verdict = "include" if f.include is True else ("exclude" if f.include is False else "pending")
        style = "green" if f.include is True else ("red" if f.include is False else "yellow")
        table.add_row(
            str(f.path),
            _human_size(f.size_bytes),
            f"[{style}]{verdict}[/{style}]",
            f.reason,
            f.os_profile,
        )

    return table


def snapshot_table(snapshots: list[SnapshotMeta]) -> Table:
    """Build a Rich table of snapshots.

    Args:
        snapshots: List of SnapshotMeta objects to display.

    Returns:
        A Rich Table ready to print.
    """
    table = Table(title="Snapshots")
    table.add_column("#", justify="right", style="dim")
    table.add_column("ID", style="cyan")
    table.add_column("Created", style="green")
    table.add_column("Trigger")
    table.add_column("Files", justify="right")
    table.add_column("Host", style="dim")

    for idx, s in enumerate(snapshots, start=1):
        table.add_row(
            str(idx),
            s.id,
            s.created_at,
            s.trigger,
            str(s.file_count),
            s.hostname,
        )

    return table


def flag_panel(flag_result: FlagResult) -> Panel:
    """Build a Rich panel summarizing a flagged file.

    Args:
        flag_result: The FlagResult to display.

    Returns:
        A Rich Panel with match details.
    """
    lines: list[str] = [f"[bold]{flag_result.config_file.path}[/bold]"]

    if flag_result.matches:
        lines.append("")
        for m in flag_result.matches:
            lines.append(f"  line {m.line_number}: [{m.pattern_name}] {m.preview}")

    if flag_result.ai_flagged:
        lines.append("")
        lines.append("  [yellow]AI flagged as potentially sensitive[/yellow]")

    return Panel("\n".join(lines), title="Sensitive File", border_style="yellow")


def _human_size(size_bytes: int) -> str:
    """Convert bytes to a human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
