"""Logging infrastructure for DotSync."""

import logging
from pathlib import Path

from rich.logging import RichHandler

LOG_DIR = Path.home() / ".dotsync"
LOG_FILE = LOG_DIR / "dotsync.log"


def setup_logging(verbose: bool = False) -> None:
    """Configure logging handlers for DotSync.

    Sets up two handlers:
    - RichHandler for console output (INFO level, or DEBUG if verbose=True)
    - FileHandler writing to ~/.dotsync/dotsync.log (DEBUG level always)

    Args:
        verbose: If True, set console handler to DEBUG level. Otherwise INFO.
    """
    logger = logging.getLogger("dotsync")
    logger.setLevel(logging.DEBUG)
    logger.handlers = []  # Clear any existing handlers

    # Console handler with Rich
    console_level = logging.DEBUG if verbose else logging.INFO
    console_handler = RichHandler(
        level=console_level,
        rich_tracebacks=True,
        show_time=False,
        show_path=False,
    )
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console_handler)

    # File handler
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(file_handler)
