"""Logging configuration for the application."""

import logging
import os
from pathlib import Path
from typing import Optional

from .datetime_string import datetime_string

# Constants
DEFAULT_LOG_FORMAT = "[%(asctime)s] %(name)s %(levelname)s: %(message)s"
DEFAULT_LOG_LEVEL = logging.INFO

def is_debug_mode() -> bool:
    """Check if the application is running in debug mode."""
    return os.getenv("DEBUG", False).lower() in (True, "true", "1", "yes", "on")

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
        import shutil
        full_log_file = os.path.abspath(self.baseFilename)
        symlink_path = os.path.join(os.path.dirname(full_log_file), self.symlink_name)

        # Remove existing file if it exists
        if os.path.exists(symlink_path):
            try:
                os.remove(symlink_path)
            except OSError:
                pass
        
        try:
            # Try symlink first (requires admin privileges on Windows)
            os.symlink(full_log_file, symlink_path)
        except (OSError, NotImplementedError):
            try:
                # Fall back to hard link
                os.link(full_log_file, symlink_path)
            except OSError:
                try:
                    # Final fallback: copy the file
                    shutil.copy2(full_log_file, symlink_path)
                except Exception:
                    pass  # Ignore if all methods fail


def configure_logging(
    log_file: Optional[Path] = None, 
    log_level: int = DEFAULT_LOG_LEVEL, 
    log_format: str = DEFAULT_LOG_FORMAT, 
    console_output: bool = True,
    force_enable: bool = False
) -> None:
    """
    Configure logging for the application.

    Args:
        log_file: Path to the log file. If None, uses default path.
        log_level: Logging level to use.
        log_format: Format string for log messages.
        console_output: Whether to also output logs to console.
        force_enable: Force enable logging even in production.
    """

    if not force_enable and not is_debug_mode():
        # Set root logger to CRITICAL to effectively disable all logging
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.CRITICAL)
        root_logger.handlers.clear()
        return
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
