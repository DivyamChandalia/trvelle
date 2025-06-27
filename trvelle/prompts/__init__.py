"""
Prompts module containing all system prompts
"""

from .researcher_instructions import RESEARCHER_INSTRUCTIONS
from .supervisor_instructions import SUPERVISOR_INSTRUCTIONS
from .tool_instructions import (
    TOOL_USAGE_INSTRUCTIONS,
    SUPERVISOR_TOOL_INSTRUCTIONS, 
    RESEARCHER_TOOL_INSTRUCTIONS,
    COMMON_ERROR_RECOVERY
)

__all__ = [
    "SUPERVISOR_INSTRUCTIONS", 
    "RESEARCHER_INSTRUCTIONS", 
    "TOOL_USAGE_INSTRUCTIONS",
    "SUPERVISOR_TOOL_INSTRUCTIONS",
    "RESEARCHER_TOOL_INSTRUCTIONS", 
    "COMMON_ERROR_RECOVERY"
]
