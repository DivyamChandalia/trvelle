from mcp.server.fastmcp import FastMCP
from fastapi import FastAPI
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, field_validator
import logging

logger = logging.getLogger(__name__)

mcp = FastMCP("ResearcherAgentTool")

class ResearcherAgentInput(BaseModel):
    """Input schema for a trip segment"""

    city: str = Field(..., description="City for the trip segment.", min_length=1)
    content: str = Field(..., description="The content plan for this trip segment.", min_length=1)
    arrival_datetime: str = Field(..., description="Arrival datetime in ISO 8601 format (YYYY-MM-DDTHH:MM:SS).")
    departure_datetime: str = Field(..., description="Departure datetime in ISO 8601 format (YYYY-MM-DDTHH:MM:SS).")
    segment_number: int = Field(..., description="Unique identifier for the trip segment for future replanning.", ge=1)
    adults: int = Field(1, description="Number of adults for the trip segment.", ge=1, le=20)
    children: Optional[int] = Field(None, description="Number of children for the trip segment.", ge=0, le=10)
    interests: Optional[List[str]] = Field(None, description="Optional list of traveler interests (e.g., ['museums', 'food']).")
    information: Optional[str] = Field(None, description="Optional string with additional context like budget or style.")
    feedback: Optional[str] = Field(None, description="Optional feedback to make changes to the trip segment.")
    
    @field_validator('arrival_datetime', 'departure_datetime')
    @classmethod
    def validate_datetime_format(cls, v):
        """Validate datetime format is ISO 8601."""
        try:
            from datetime import datetime
            datetime.fromisoformat(v)
        except ValueError:
            raise ValueError("Datetime must be in ISO 8601 format (YYYY-MM-DDTHH:MM:SS)")
        return v

class ResearcherAgent:
    """A tool for creating and managing trip segments through a researcher agent."""

    def process_trip_segment(
        self,
        city: str,
        content: str,
        arrival_datetime: str,
        departure_datetime: str,
        segment_number: int,
        adults: int = 1,
        children: Optional[int] = None,
        interests: Optional[List[str]] = None,
        information: Optional[str] = None,
        feedback: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Process a trip segment creation request and return structured trip segment information.
        """
        validated = ResearcherAgentInput.model_validate({
            'city': city,
            'content': content,
            'arrival_datetime': arrival_datetime,
            'departure_datetime': departure_datetime,
            'segment_number': segment_number,
            'adults': adults,
            'children': children,
            'interests': interests,
            'information': information,
            'feedback': feedback,
        })
        
        logger.info(f"Created trip segment #{segment_number} for {city}")
        
        # Add success indicator and metadata
        result = validated.model_dump()
        result.update({
            "success": True,
            "instance_id": segment_number,
            "research_segments": [result]  # Wrap in list for compatibility with orchestrator
        })
        
        return result

researcher_agent_instance = ResearcherAgent()

@mcp.tool()
async def researcher_agent(
    city: str,
    content: str,
    arrival_datetime: str,
    departure_datetime: str,
    segment_number: int,
    adults: int = 1,
    children: Optional[int] = None,
    interests: Optional[List[str]] = None,
    information: Optional[str] = None,
    feedback: Optional[str] = None,
):
    """
    Invokes a research agent to create a structured trip segment with detailed travel information 
    including destination, dates, group size, and preferences. Helps organize and plan individual 
    parts of a larger trip itinerary by researching and gathering relevant information.

    Args:
        city (str): The destination city for the trip segment.
        content (str): The main content/description of the trip segment.
        arrival_datetime (str): The arrival date and time at the destination.
        departure_datetime (str): The departure date and time from the destination.
        segment_number (int): Identifier number for the trip segment.
        adults (int): Number of adult travelers.
        children (Optional[int]): Number of child travelers, if any.
        interests (Optional[List[str]]): List of traveler interests/preferences.
        information (Optional[str]): Additional relevant information for the trip segment.
        feedback (Optional[str]): Feedback for modifying the trip segment.
    """
    result = researcher_agent_instance.process_trip_segment(
        city=city,
        content=content,
        arrival_datetime=arrival_datetime,
        departure_datetime=departure_datetime,
        segment_number=segment_number,
        adults=adults,
        children=children,
        interests=interests,
        information=information,
        feedback=feedback
    )
    return result

async def main():
    sample_segment = {
        "city": "Paris",
        "content": "Explore the romantic side of Paris with visits to iconic landmarks",
        "arrival_datetime": "2025-07-01T10:00:00",
        "departure_datetime": "2025-07-03T18:00:00",
        "segment_number": 1,
        "adults": 2,
        "children": 0,
        "interests": ["museums", "food", "romance"],
        "information": "Budget: $1500, prefer central location",
        "feedback": None
    }
    print(await researcher_agent(**sample_segment))

# if __name__ == "__main__":
#     import asyncio
#     asyncio.run(main())
