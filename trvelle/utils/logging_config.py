"""Logging configuration for the application."""

import logging
import os
from pathlib import Path
from typing import Optional

from .datetime_string import datetime_string

# Constants
DEFAULT_LOG_FORMAT = "[%(asctime)s] %(name)s %(levelname)s: %(message)s"
DEFAULT_LOG_LEVEL = logging.INFO


def get_log_file_path() -> Path:
    """Get the path to the log file."""
    script_dir = Path(__file__).parent.parent.parent
    log_dir = script_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    return log_dir / f"agent_run_{datetime_string()}.log"


class SymlinkUpdateHandler(logging.FileHandler):
    def __init__(self, filename, symlink_name="latest.log", **kwargs):
        self.symlink_name = symlink_name
        super().__init__(filename, **kwargs)

        self.update_symlink()

    def update_symlink(self):
        full_log_file = os.path.abspath(self.baseFilename)
        symlink_path = os.path.join(os.path.dirname(full_log_file), self.symlink_name)

        if os.path.exists(symlink_path):
            os.remove(symlink_path)
        os.symlink(full_log_file, symlink_path)


def configure_logging(
    log_file: Optional[Path] = None, log_level: int = DEFAULT_LOG_LEVEL, log_format: str = DEFAULT_LOG_FORMAT, console_output: bool = True
) -> None:
    """
    Configure logging for the application.

    Args:
        log_file: Path to the log file. If None, uses default path.
        log_level: Logging level to use.
        log_format: Format string for log messages.
        console_output: Whether to also output logs to console.
    """
    log_file = log_file or get_log_file_path()
    formatter = logging.Formatter(log_format)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    root_logger.handlers.clear()

    file_handler = SymlinkUpdateHandler(log_file, symlink_name="latest_agent_run.log")
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    if console_output:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    # Suppress noisy HTTP and MCP logs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("mcp.client.streamable_http").setLevel(logging.WARNING)
    logging.getLogger("mcp.client").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    root_logger.info(f"Logging configured. Log file: {log_file}")


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance with the given name.

    Args:
        name: Name of the logger.

    Returns:
        Logger instance.
    """
    return logging.getLogger(name)
