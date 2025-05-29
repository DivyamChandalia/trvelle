"""Environment configuration for the application."""

import os
from pathlib import Path
from typing import Dict, Optional

from .logging_config import get_logger

logger = get_logger(__name__)

REQUIRED_ENV_VARS = ["GOOGLE_API_KEY"]

module_path = Path(__file__).resolve().parent.parent.parent
env_file_path = module_path / ".env"
def load_environment(env_file: Optional[Path] = env_file_path) -> Dict[str, str]:
    """
    Load environment variables from file and/or system environment.

    Args:
        env_file: Optional path to environment file.

    Returns:
        Dictionary of loaded environment variables.

    Raises:
        ValueError: If required environment variables are missing.
    """
    env_vars = {}
    print(f"Loading environment variables from {env_file}")
    if env_file and env_file.exists():
        logger.info(f"Loading environment variables from {env_file}")
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    try:
                        key, value = line.split("=", 1)
                        os.environ[key.strip()] = value.strip()
                        env_vars[key.strip()] = value.strip()
                    except ValueError:
                        logger.warning(f"Skipping invalid line in env file: {line}")

    for var in REQUIRED_ENV_VARS:
        value = os.getenv(var)
        if value:
            env_vars[var] = value
        else:
            logger.warning(f"Required environment variable {var} not found")

    missing_vars = [var for var in REQUIRED_ENV_VARS if var not in env_vars]
    if missing_vars:
        raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")

    logger.info("Environment variables loaded successfully")
    return env_vars


def get_env(key: str, default: Optional[str] = None) -> Optional[str]:
    """
    Get an environment variable value.

    Args:
        key: Environment variable key.
        default: Default value if not found.

    Returns:
        Environment variable value or default.
    """
    return os.getenv(key, default)
