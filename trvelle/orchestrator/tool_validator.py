"""
Tool validation and error handling for MCP tools.
Provides centralized validation to prevent malformed tool calls from reaching tools.
"""
import logging
from typing import Dict, Any, List, Optional, Tuple
from pydantic import BaseModel, ValidationError
from langchain_core.messages import ToolMessage

# Import all tool input schemas
from ..tools.flight_search import FlightSearchInput
from ..tools.hotel_search import HotelSearchInput  
from ..tools.itinerary_tool import Itinerary
from ..tools.researcher_agent_tool import ResearcherAgentInput
from ..tools.trip_segment_tool import TripSegmentInput

logger = logging.getLogger(__name__)

# Map tool names to their input schemas
TOOL_SCHEMAS = {
    "flight_search": FlightSearchInput,
    "hotel_search": HotelSearchInput,
    "itinerary_tool": Itinerary,
    "researcher_agent": ResearcherAgentInput,
    "trip_segment": TripSegmentInput,
    # External tools that don't need validation
    "tavily_search": None,
}

class ToolValidationError(Exception):
    """Custom exception for tool validation errors."""
    def __init__(self, tool_name: str, error_details: str):
        self.tool_name = tool_name
        self.error_details = error_details
        super().__init__(f"Tool '{tool_name}' validation failed: {error_details}")

class ToolValidator:
    """Validates tool calls before execution."""
    
    def __init__(self):
        self.tool_schemas = TOOL_SCHEMAS
    
    def validate_tool_exists(self, tool_name: str, available_tools: Dict[str, Any]) -> bool:
        """Check if tool exists in available tools."""
        return tool_name in available_tools
    
    def validate_tool_input(self, tool_name: str, tool_args: Dict[str, Any]) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        """
        Validate tool input against its schema.
        
        Returns:
            Tuple of (is_valid, error_message, validated_args)
        """
        # Skip validation for external tools without schemas
        if self.tool_schemas.get(tool_name) is None:
            return True, None, tool_args
        
        schema = self.tool_schemas.get(tool_name)
        if not schema:
            return False, f"No validation schema found for tool '{tool_name}'", None
        
        try:
            # Validate using Pydantic schema
            if tool_name == "flight_search":
                # FlightSearchInput expects search_params key
                if "search_params" in tool_args:
                    validated = schema.model_validate(tool_args["search_params"])
                    return True, None, {"search_params": validated.model_dump()}
                else:
                    validated = schema.model_validate(tool_args)
                    return True, None, {"search_params": validated.model_dump()}
            
            elif tool_name == "hotel_search":
                # HotelSearchInput expects search_params key
                if "search_params" in tool_args:
                    validated = schema.model_validate(tool_args["search_params"])
                    return True, None, {"search_params": validated.model_dump()}
                else:
                    validated = schema.model_validate(tool_args)
                    return True, None, {"search_params": validated.model_dump()}
            
            elif tool_name == "itinerary_tool":
                # Itinerary tool expects itinerary key
                if "itinerary" in tool_args:
                    validated = schema.model_validate(tool_args["itinerary"])
                    return True, None, {"itinerary": validated.model_dump()}
                else:
                    validated = schema.model_validate(tool_args)
                    return True, None, {"itinerary": validated.model_dump()}
            
            else:
                # For researcher_agent and trip_segment, validate directly
                validated = schema.model_validate(tool_args)
                return True, None, validated.model_dump()
                
        except ValidationError as e:
            error_msg = self._format_validation_error(e)
            return False, error_msg, None
        except Exception as e:
            return False, f"Unexpected validation error: {str(e)}", None
    
    def _format_validation_error(self, error: ValidationError) -> str:
        """Format Pydantic validation error for user-friendly output."""
        errors = []
        for err in error.errors():
            field = ".".join(str(x) for x in err["loc"])
            msg = err["msg"]
            errors.append(f"Field '{field}': {msg}")
        return "; ".join(errors)
    
    def create_error_response(self, tool_call_id: str, tool_name: str, error_message: str) -> ToolMessage:
        """Create a standardized error response for failed tool calls."""
        error_content = {
            "error": True,
            "tool_name": tool_name,
            "message": f"Tool call failed: {error_message}",
            "suggestion": self._get_error_suggestion(tool_name, error_message)
        }
        
        return ToolMessage(
            content=str(error_content),
            tool_call_id=tool_call_id,
            name=tool_name
        )
    
    def _get_error_suggestion(self, tool_name: str, error_message: str) -> str:
        """Provide helpful suggestions based on common error patterns."""
        suggestions = {
            "flight_search": "Ensure you provide either flight_legs for multi-city OR departure_id/arrival_id with outbound_date for one-way/round-trip searches. Check date format (YYYY-MM-DD). Available to: supervisor agents only.",
            "hotel_search": "Ensure you provide 'q' (search query), 'check_in_date', and 'check_out_date' in YYYY-MM-DD format. Available to: researcher agents only.",
            "itinerary_tool": "Ensure your itinerary has 'trip_name', 'summary' with dates, and 'daily_plan' with properly structured items. Available to: supervisor agents only.",
            "researcher_agent": "Ensure you provide 'city', 'content', 'arrival_datetime', 'departure_datetime' in ISO format, and 'segment_number'. Available to: supervisor agents only.",
            "trip_segment": "Ensure you provide 'city', 'content', 'arrival_datetime', 'departure_datetime' in ISO format. Available to: researcher agents only.",
            "tavily_search": "Available to both supervisor and researcher agents for web search functionality."
        }
        
        base_suggestion = suggestions.get(tool_name, "Check the tool documentation for required parameters.")
        
        # Add specific suggestions based on error content
        if "not available for" in error_message.lower():
            return f"This tool is not available for this agent type. {base_suggestion}"
        elif "required" in error_message.lower():
            return f"{base_suggestion} Missing required fields detected."
        elif "format" in error_message.lower() or "invalid" in error_message.lower():
            return f"{base_suggestion} Check data format and types."
        else:
            return base_suggestion

# Create global validator instance
tool_validator = ToolValidator()
