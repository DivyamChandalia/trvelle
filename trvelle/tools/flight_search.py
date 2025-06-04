import os
from mcp.server.fastmcp import FastMCP
from fastapi import FastAPI
import asyncio
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
import json
from serpapi import GoogleSearch
# from ..utils import get_logger
# logger = get_logger(__name__)

mcp = FastMCP("FlightSearch")

class FlightLeg(BaseModel):
    """Schema for a single leg in a multi-city flight search."""
    departure_id: str = Field(..., description="Departure airport code(s) for this leg (e.g., 'SFO' or 'SFO,ONT').")
    arrival_id: str = Field(..., description="Arrival airport code(s) for this leg (e.g., 'JFK,LGA').")
    date: str = Field(..., description="Date for this flight leg in YYYY-MM-DD format.")
    times: Optional[str] = Field("0,23,0,23", description="String defining departure (required, 2 numbers) and optional arrival (2 numbers) hourly ranges (0-23) as comma-separated integers, e.g., 4,18,3,19 for 4:00 AM-7:00 PM departure, 3:00 AM-8:00 PM arrival.")

class FlightSearchInput(BaseModel):
    """Input schema for the Flight Search tool. Provide either a list of multi-city legs OR departure/arrival IDs with dates for one-way or round-trip. """
    # --- Parameters for One-Way / Round-Trip ---
    departure_id: Optional[str] = Field(None, description="Departure airport code(s) (e.g., 'DEL', 'DEL,BOM') for one-way/round-trip.")
    arrival_id: Optional[str] = Field(None, description="Arrival airport code(s) (e.g., 'CDG', 'CDG,LYS') for one-way/round-trip.")
    outbound_date: Optional[str] = Field(None, description="Outbound date in YYYY-MM-DD format for one-way/round-trip.")
    return_date: Optional[str] = Field(None, description="Return date in YYYY-MM-DD format. If provided, performs a round-trip search.")

    # --- Parameters for Multi-City ---
    flight_legs: Optional[List[FlightLeg]] = Field(None, description="List of flight legs for a multi-city search. Use this *instead* of departure_id/arrival_id/dates.")

    # --- Common Parameters ---
    adults: int = Field(1, description="Number of adult passengers.")
    children: int = Field(0, description="Number of child passengers (age 2-11).")
    infants_in_seat: int = Field(0, description="Number of infants (under 2) requiring their own seat.")
    infants_on_lap: int = Field(0, description="Number of infants (under 2) travelling on lap.")
    travel_class: int = Field(1, description="Travel class: 1=Economy, 2=Premium Economy, 3=Business, 4=First.")
    currency: str = Field("USD", description="Currency code for pricing (e.g., 'USD', 'EUR', 'INR').")
    sort_by: Optional[int] = Field(
        1,
        description=(
            "Determines which flight or set is returned based on ranking: "
            "1=Overall best (default), 2=Cheapest, 3=Departure earliest, "
            "4=Arrival earliest, 5=Shortest duration, 6=Lowest emissions."
        )
    )

class FlightSearch:
    def __init__(self):
        self.api_key = os.getenv("SERPAPI_API_KEY")
        self.use_cache = True

    def format_flight_data_simple(self, data):
        output = []
        for i, option in enumerate(data):
            output.append(f"**Leg {i+1}:**")
            total_duration_hours = option['total_duration'] // 60
            total_duration_minutes = option['total_duration'] % 60
            output.append(f"The total travel time is about {total_duration_hours} hours and {total_duration_minutes} minutes.")
            
            outbound_flights = option['flights'] # Assuming outbound is the first part before the first layover
            for i, flight in enumerate(outbound_flights):
                output.append(f"- Flight {i + 1}:")
                output.append(f"  Flying from {flight['departure_airport']['name']} ({flight['departure_airport']['id']}) at {flight['departure_airport']['time'].split(' ')[1]} on {flight['departure_airport']['time'].split(' ')[0]} to {flight['arrival_airport']['name']} ({flight['arrival_airport']['id']}) at {flight['arrival_airport']['time'].split(' ')[1]} on {flight['arrival_airport']['time'].split(' ')[0]}.")
                output.append(f"  With {flight['airline']} (Flight {flight['flight_number']}).")
                duration_hours = flight['duration'] // 60
                duration_minutes = flight['duration'] % 60
                output.append(f"  This flight takes about {duration_hours} hours and {duration_minutes} minutes.")
                output.append(f"  Legroom is {flight['legroom'].lower().replace(' in', ' inches')}.")
                if 'often_delayed_by_over_30_min' in flight and flight['often_delayed_by_over_30_min']:
                    output.append("  Note: This flight is often delayed by over 30 minutes.")
                if 'extensions' in flight:
                    features = [ext for ext in flight['extensions'] if 'Carbon emissions estimate' not in ext and 'legroom' not in ext]
                    if features:
                        output.append("  Features: " + ", ".join(features))
            if 'layovers' in option and option['layovers']:
                output.append("Layover:")
                for layover in option['layovers']:
                    layover_duration_hours = layover['duration'] // 60
                    layover_duration_minutes = layover['duration'] % 60
                    output.append(f"- You have a stop at {layover['name']} ({layover['id']}) for about {layover_duration_hours} hours and {layover_duration_minutes} minutes.")

            output.append("-" * 20) # Separator
            output.append("") # Add a blank line for readability
        output.append(f"This is a {option['type']} flight costing {option['price']} {option["currency"]} in total.")
        return "\n".join(output)
    
    def _extract_flight_list(self, results):
        """Get best_flights or fallback to other_flights."""
        flights = results.get("best_flights", [])
        if not flights:
            flights = results.get("other_flights", [])
        return flights[0] if flights else []
    
    def _fetch_results(self, params, cache_file):
        """Loads from cache_file if present, otherwise calls SerpAPI and caches."""
        if self.use_cache:
            if os.path.exists(cache_file):
                with open(cache_file, "r") as f:
                    return json.load(f)
        # print(params)
        search = GoogleSearch(params)
        results = search.get_dict()
        # print(results)
        if self.use_cache:
            with open(cache_file, "w") as f:
                json.dump(results, f, indent=2)
        return results

    def handle_search(self, params):
        """Fetch outbound then inbound for each token."""
        raw = self._fetch_results(params, "cache/two_way.json")
        first_leg = self._extract_flight_list(raw)
        all_returns = [first_leg]
        while first_leg.get("departure_token") is not None:
            params["departure_token"] = first_leg["departure_token"]
            inbound = self._fetch_results(params, "cache/return_way.json")
            return_flights = self._extract_flight_list(inbound)
            if not return_flights:
                break
            all_returns.append(return_flights)
            first_leg = return_flights

        all_returns[-1]["currency"] = params.get("currency", "USD")
        output = self.format_flight_data_simple(all_returns)

        return output, raw
    

    def flight_search(self, search_params: FlightSearchInput):
        if "flight_legs" in search_params and search_params["flight_legs"]:
            search_params["multi_city_json"] = json.dumps([leg for leg in search_params['flight_legs']])
            del search_params["flight_legs"]
            search_params["type"] = 3
        elif "return_date" in search_params and search_params["return_date"]:
            search_params["type"] = 1
        else:
            search_params["type"] = 2
        
        search_params["engine"] = "google_flights"
        search_params["api_key"] = self.api_key
        search_params["deep_search"] = True
        result = self.handle_search(search_params)
        return result
        

searcher = FlightSearch()

@mcp.tool()
async def flight_search(search_params: FlightSearchInput):
    """
    Performs a flight search based on the provided parameters.

    Args:
        search_params (FlightSearchInput): An instance of the FlightSearchInput
            Pydantic model containing all search criteria.
    """
    if isinstance(search_params, FlightSearchInput):
        search_params_dict = search_params.model_dump()
    else:
        search_params_dict = search_params

    results = searcher.flight_search(search_params_dict)
    return results


async def main():
    search_params = {
        "flight_legs": [{"departure_id":"CDG","arrival_id":"NRT","date":"2025-06-01"},{"departure_id":"NRT","arrival_id":"LAX,SEA","date":"2025-06-08"},{"departure_id":"LAX,SEA","arrival_id":"AUS","date":"2025-06-15","times":"8,18,9,23"}],
        "adults": 2
    }
    print(await flight_search(search_params))

# if __name__ == "__main__":
    # asyncio.run(main())

# mcp.run(transport="streamable-http")
