from mcp.server.fastmcp import FastMCP
from fastapi import FastAPI
from typing import List, Optional, Dict, Any, Literal
from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator
from datetime import datetime, date, timedelta
from datetime import date as CalendarDate
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

class ItemCost(BaseModel):
    price: float = Field(ge=0, allow_inf_nan=False, description='Numeric cost; zero only when explicitly free. Omit cost entirely when unknown.')
    currency: str = Field(pattern=r'^[A-Z]{3}$')
    scope: Literal['per_person', 'party']
    status: Literal['quoted', 'estimate'] = 'estimate'
    source_url: Optional[str] = None
    coverage_key: Optional[str] = Field(None, description='Shared identifier for a combined ticket or pass, so its price is counted once across covered activities.')


class PlannedItem(BaseModel):
    model_config = {'extra': 'allow'}
    item_type: Literal['card', 'tag'] = 'card'
    card_type: Literal['flight', 'hotel', 'activity', 'transfer', 'meal', 'free_time', 'note'] = 'activity'
    title: Optional[str] = None
    description: Optional[str] = None
    uid: Optional[str] = None
    location: Optional[str] = None
    place_name: Optional[str] = Field(None, description='Exact attraction name for place/photo matching, especially when the card title combines attractions or describes a walk. Omit for generic rest or orientation items.')
    image_url: Optional[str] = Field(None, description='Optional actual matched-place photo URL from provider results. Never invent a photo URL.')
    photos: Optional[List[Dict[str, Any]]] = Field(None, description='Optional provider-sourced matched-place photos with url, thumbnail and source_url.')
    place_details: Optional[Dict[str, Any]] = Field(None, description='Optional structured provider result for a place matched by name and location; keep source, address, coordinates, rating and hours when available.')
    visitor_information: Optional[str | Dict[str, Any]] = Field(None, validation_alias=AliasChoices('visitor_information', 'visitor_details'), description="Optional sourced visit notes: opening hours/closures, ticket or reservation rules, dress code and accessibility where relevant. Use plain text or a dictionary of notes; keep uncertain or future-date information explicitly unconfirmed. Omit for ordinary strolls/rest when no special visitor rules apply.")
    source_url: Optional[str] = Field(None, description="Official attraction or other source URL supporting visit notes, if researched. Do not invent a URL.")
    visitor_information_sources: Optional[List[Dict[str, str]]] = Field(None, description="Supporting researched sources, each with title and url, when available.")
    cost: Optional[ItemCost] = Field(None, description='Optional researched ticket, meal or transport cost. Specify currency, party/per-person scope and quote/estimate status. Never invent an unavailable price or put prices in visitor notes.')

    @field_validator('title', 'description', mode='before')
    @classmethod
    def readable_prose(cls, value):
        from trvelle.utils.travel_text import readable_text
        return readable_text(value)
    transport_mode: Optional[str] = Field(None, description="Chosen transport mode: walking, car (including taxi), train (including metro), bus, bicycle, boat (including ferry), or flight. Leave null when unknown or when listing alternatives.")
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    timezone: Optional[str] = None
    time_status: Literal['suggested', 'verified', 'unknown'] = 'suggested'


class DailyPlan(BaseModel):
    day: int = Field(description="Day number of the itinerary (e.g., 1, 2, ...)")
    date: Optional[CalendarDate] = None
    destination: Optional[str] = None
    items: List[PlannedItem] = Field(description="Typed flight/hotel/activity/transfer/meal/free_time/note items. Activities need a location and suggested start/end time. Only actual place activities need visitor details.")

    @field_validator("items", mode="before")
    @classmethod
    def preprocess_llm_items(cls, v: List[Any]) -> List[Dict[str, Any]]:
        processed_items = []
        if not isinstance(v, list):
            return v

        for item_input in v:
            if isinstance(item_input, dict):
                item_input = dict(item_input)
                if item_input.get('time') and not item_input.get('start_time'):
                    item_input['start_time'] = str(item_input['time'])
                if item_input.get('item_type') == 'tag':
                    item_input.setdefault('card_type', 'note')
            if isinstance(item_input, dict) and "description" in item_input:
                if len(item_input) == 1 or \
                   ("title" not in item_input and "item_type" not in item_input and "card_type" not in item_input):
                    processed_items.append({**item_input,
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
    @model_validator(mode='after')
    def ordered(self):
        if self.end < self.start:
            raise ValueError('Trip end must be on or after its start.')
        return self

class TripSummary(BaseModel):
    dates: TripDates
    origin: Optional[str] = None
    budget: Optional[str] = None
    budget_amount: Optional[float] = Field(None, ge=0)
    currency: Optional[str] = None
    travelers: int = Field(1, ge=1)
    vibe: Optional[str] = None

class TripRequirements(BaseModel):
    private_room: Optional[bool] = None
    bed: Optional[Literal['double', 'twin', 'any']] = None
    minimum_rating: Optional[float] = Field(None, ge=1, le=5)
    checked_baggage_kg: Optional[float] = Field(None, ge=0)

class Itinerary(BaseModel):
    model_config = {'extra': 'allow'}
    trip_name: str
    planning_status: Literal['complete', 'partial'] = Field('complete', description='Use partial when required flight/hotel searches or pricing could not finish. Publish the researched schedule as a draft rather than dropping it into chat only.')
    unfinished: List[str] = Field(default_factory=list, description='Concrete remaining work for a partial plan, such as Hotels or Full trip budget. Do not invent missing quotes.')
    trip_description: Optional[str] = None
    summary: TripSummary
    requirements: TripRequirements = Field(default_factory=TripRequirements, description='Traveler constraints, distinct from verified provider details. Preserve requested private room, bed configuration and minimum review rating.')
    daily_plan: List[DailyPlan] = Field(default_factory=list)

    @model_validator(mode='after')
    def fill_missing_day_dates(self):
        start = self.summary.dates.start
        for index, day in enumerate(self.daily_plan):
            if day.date is None:
                day.date = start + timedelta(days=index)
        return self

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
        
        # If the input already has the 'itinerary' wrapper, use it directly
        model = Itinerary.model_validate(itinerary_data)
        return model.model_dump(mode="json")

validator = ItineraryValidator()

@mcp.tool()
async def itinerary_tool(itinerary: Itinerary) -> Dict[str, Any]:
    """
    Validate and structure a trip itinerary according to the Itinerary schema.
    Input must be a JSON object matching Itinerary (with top-level 'itinerary').
    
    Args:
        itinerary (Itinerary) -> Dict[str, Any]: The itinerary data to validate and format
    """
    result = validator.validate_and_format_itinerary(itinerary)
    itinerary_uid = str(uuid.uuid4())
    result["itinerary_uid"] = itinerary_uid
    return {"result": f"Itinerary displayed with UID:{itinerary_uid}", "raw": result}

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
