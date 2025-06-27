from mcp.server.fastmcp import FastMCP
from fastapi import FastAPI
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import datetime, date
import logging
import yaml
import uuid

logger = logging.getLogger(__name__)

mcp = FastMCP("ItineraryTool")

# --- Item Base Models ---

class ItineraryItem(BaseModel):
    item_type: str = Field(description="Can either be a tag or a card")

class CardItem(ItineraryItem):
    item_type: str = "card"
    title: Optional[str] = Field(None, description="Title of the card, can be derived from description if not provided by LLM")
    card_type: str = Field(description="Type of card: flight, hotel, or activity")
    description: Optional[str] = Field(None, description="Common description field for all cards")

    @model_validator(mode='after')
    def ensure_title_for_card(self) -> 'CardItem':
        if not self.title and self.description:
            self.title = (self.description[:47] + "...") if len(self.description) > 50 else self.description
        elif not self.title and not self.description:
            raise ValueError(f"Card of type '{self.card_type}' must have a title or a description to derive a title.")
        return self

# --- Specific Item Models ---

class TagItem(ItineraryItem):
    item_type: str = "tag"
    description: str = Field(description="Content of the tag (e.g., 'Arrive at airport at 9pm')")

class FlightCard(CardItem):
    card_type: str = "flight"
    uid: Optional[str] = Field(None, description="UID for chosen flight")

class HotelCard(CardItem):
    card_type: str = "hotel"
    uid: Optional[str] = Field(None, description="UID for chosen hotel")

class ActivityCard(CardItem):
    card_type: str = "activity"
    start_time: Optional[datetime] = Field(None, description="Activity start time")
    end_time: Optional[datetime] = Field(None, description="Activity end time")

# --- Daily Plan Model ---

class DailyPlan(BaseModel):
    day: int = Field(description="Day number of the itinerary (e.g., 1, 2, ...)")
    items: List[Dict[str, Any]] = Field(description="List of items planned for the day")

    @field_validator("items", mode="before")
    @classmethod
    def preprocess_llm_items(cls, v: List[Any]) -> List[Dict[str, Any]]:
        processed_items = []
        if not isinstance(v, list):
            return v

        for item_input in v:
            if isinstance(item_input, dict) and "description" in item_input:
                if len(item_input) == 1 or \
                   ("title" not in item_input and "item_type" not in item_input and "card_type" not in item_input):
                    processed_items.append({
                        "item_type": "card",
                        "card_type": "activity",
                        "title": item_input["description"],
                        "description": item_input["description"]
                    })
                    continue

                if item_input.get("item_type") == "card" and "title" not in item_input:
                    item_input["title"] = item_input["description"]
                    processed_items.append(item_input)
                    continue
                
                if item_input.get("item_type") == "tag" and "content" not in item_input:
                    item_input["content"] = item_input["description"]
                    processed_items.append(item_input)
                    continue

            processed_items.append(item_input)
        return processed_items

# --- Trip Models ---

class TripDates(BaseModel):
    start: date
    end: date

class TripSummary(BaseModel):
    dates: TripDates
    origin: Optional[str] = None
    budget: Optional[str] = None
    travelers: int = 0
    vibe: Optional[str] = None

class Itinerary(BaseModel):
    trip_name: str
    trip_description: Optional[str] = None
    summary: TripSummary
    daily_plan: List[DailyPlan] = Field(default_factory=list)

    @field_validator('daily_plan')
    def validate_daily_plan_day_order(cls, daily_plan):
        if not daily_plan:
            return daily_plan
        expected_day = 1
        for plan in daily_plan:
            if plan.day != expected_day:
                raise ValueError(f"Daily plans must be in sequential order starting from 1. Expected day {expected_day}, found {plan.day}")
            expected_day += 1
        return daily_plan

class ItineraryValidator:
    def __init__(self):
        pass

    def validate_and_format_itinerary(self, itinerary_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate the provided itinerary data and return the structured itinerary.
        """
        logger.info("Validating itinerary data against schema...")
        
        try:
            # If the input already has the 'itinerary' wrapper, use it directly

            model = Itinerary.model_validate(itinerary_data)
            
            return model.model_dump()
        except Exception as e:
            logger.error(f"Error validating itinerary: {e}")
            return {"error": str(e), "message": "Failed to validate itinerary"}

validator = ItineraryValidator()

@mcp.tool()
async def itinerary_tool(itinerary: Itinerary) -> Dict[str, Any]:
    """
    Validate and structure a trip itinerary according to the Itinerary schema.
    Input must be a JSON object matching Itinerary (with top-level 'itinerary').
    
    Args:
        itinerary (Itinerary) -> Dict[str, Any]: The itinerary data to validate and format
    """
    try:
        result = validator.validate_and_format_itinerary(itinerary)
        itinerary_uid = str(uuid.uuid4())
        result["itinerary_uid"] = itinerary_uid
        if "error" in result:
            return {"error": result["error"], "message": "Failed to validate itinerary"}
        return  f"Itinerary displayed with UID:{itinerary_uid}", result
    except Exception as e:
        return {"error": str(e), "message": "Failed to validate itinerary"}

async def main():
    sample_itinerary = {
        "trip_name": "Paris Adventure",
        "trip_description": "A wonderful trip to Paris",
        "summary": {
            "dates": {
                "start": "2025-07-01",
                "end": "2025-07-05"
            },
            "origin": "New York",
            "budget": "$2000",
            "travelers": 2,
            "vibe": "romantic"
        },
        "daily_plan": [
            {
                "day": 1,
                "items": [
                    {
                        "description": "Arrive at Charles de Gaulle Airport"
                    }
                ]
            }
        ]
    }
    print(await itinerary_tool(sample_itinerary))

# if __name__ == "__main__":
#     import asyncio
#     asyncio.run(main())
