from mcp.server.fastmcp import FastMCP
from fastapi import FastAPI
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, field_validator
import logging

logger = logging.getLogger(__name__)

mcp = FastMCP("TripSegmentTool")

class TripSegmentInput(BaseModel):
    """Input schema for the Trip Segment Planner tool."""
    city: str = Field(..., description="The city for the trip segment.", min_length=1)
    content: str = Field(..., description="The detailed plan for this trip segment include information about chosen hotels(including UUID), activities, etc.", min_length=1)
    arrival_datetime: str = Field(..., description="Arrival datetime in ISO 8601 format (YYYY-MM-DDTHH:MM:SS).")
    departure_datetime: str = Field(..., description="Departure datetime in ISO 8601 format (YYYY-MM-DDTHH:MM:SS).")
    adults: int = Field(1, description="Number of adults.", ge=1, le=20)
    interests: Optional[List[str]] = Field(None, description="Optional list of interests (e.g., ['museums', 'food']).")
    information: Optional[str] = Field(None, description="Optional string containing user preferences like budget, style, specific requests.")
    query: Optional[str] = Field(None, description="The query for the Supervisor if you need to clarify some information only fill this field if you have a query.")
    
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

class TripSegmentProcessor:
    """A tool for creating and managing trip segments."""

    def __init__(self):
        self.num_segments = 0

    def create_trip_segment(
        self,
        city: str,
        content: str,
        arrival_datetime: str,
        departure_datetime: str,
        adults: int = 1,
        interests: Optional[List[str]] = None,
        information: Optional[str] = None,
        query: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a trip segment with validated input data.
        """
        # Validate input against TripSegmentInput schema
        validated = TripSegmentInput.model_validate({
            'city': city,
            'content': content,
            'arrival_datetime': arrival_datetime,
            'departure_datetime': departure_datetime,
            'adults': adults,
            'interests': interests,
            'information': information,
            'query': query,
        })
        
        self.num_segments += 1
        logger.info(f"Created trip segment #{self.num_segments} for {city}")
        
        # Return the structured segment data
        result = validated.model_dump()
        result.update({
            "success": True,
            "segment_id": self.num_segments,
        })
        
        return result

trip_segment_processor = TripSegmentProcessor()

@mcp.tool()
async def trip_segment(
    city: str,
    content: str,
    arrival_datetime: str,
    departure_datetime: str,
    adults: int = 1,
    interests: Optional[List[str]] = None,
    information: Optional[str] = None,
    query: Optional[str] = None,
):
    """
    Creates a trip segment for the itinerary with validated input data.
    
    Args:
        city (str): The city for the trip segment.
        content (str): The detailed plan for this trip segment.
        arrival_datetime (str): Arrival datetime in ISO 8601 format (YYYY-MM-DDTHH:MM:SS).
        departure_datetime (str): Departure datetime in ISO 8601 format (YYYY-MM-DDTHH:MM:SS).
        adults (int): Number of adults.
        interests (Optional[List[str]]): Optional list of interests (e.g., ['museums', 'food']).
        information (Optional[str]): Optional string containing user preferences like budget, style, specific requests.
        query (Optional[str]): The query for the Supervisor if you need to clarify some information.
    """
    result = trip_segment_processor.create_trip_segment(
        city=city,
        content=content,
        arrival_datetime=arrival_datetime,
        departure_datetime=departure_datetime,
        adults=adults,
        interests=interests,
        information=information,
        query=query
    )
    return result

async def main():
    sample_segment = {
        "city": "Rome",
        "content": "Explore ancient Roman history and enjoy authentic Italian cuisine",
        "arrival_datetime": "2025-07-05T14:30:00",
        "departure_datetime": "2025-07-07T12:00:00",
        "adults": 2,
        "interests": ["history", "food", "architecture"],
        "information": "Budget: $800, prefer walking tours",
        "query": None
    }
    print(await trip_segment(**sample_segment))

# if __name__ == "__main__":
#     import asyncio
#     asyncio.run(main())
