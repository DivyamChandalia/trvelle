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

mcp = FastMCP("HotelSearch")

class HotelSearchInput(BaseModel):
    """Input schema for the Hotel Search tool."""
    q: str = Field(..., description="Search query - anything you would use in a regular Google Hotels search")
    check_in_date: str = Field(..., description="Check-in date in YYYY-MM-DD format")
    check_out_date: str = Field(..., description="Check-out date in YYYY-MM-DD format")
    currency: Optional[str] = Field("USD", description="Currency of returned prices (default: USD)")
    adults: Optional[int] = Field(2, description="Number of adults (default: 2)")
    children: Optional[int] = Field(0, description="Number of children (default: 0)")
    children_ages: Optional[str] = Field(None, description="Ages of children (1-17), comma-separated for multiple children (e.g., '5,8,10')")
    sort_by: Optional[int] = Field(
        None,
        description=(
            "Sorting criteria: "
            "3=Lowest price, 8=Highest rating, 13=Most reviewed (default: Relevance)"
        )
    )
    min_price: Optional[int] = Field(None, description="Lower bound of price range")
    max_price: Optional[int] = Field(None, description="Upper bound of price range")
    rating: Optional[int] = Field(
        None,
        description="Filter by rating: 7=3.5+, 8=4.0+, 9=4.5+"
    )
    hotel_class: Optional[str] = Field(
        None,
        description="Hotel class filter: 2=2-star, 3=3-star, 4=4-star, 5=5-star. Comma-separated for multiple (e.g., '2,3,4')"
    )
    free_cancellation: Optional[bool] = Field(None, description="Show only results with free cancellation")
    special_offers: Optional[bool] = Field(None, description="Show only results with special offers")
    vacation_rentals: Optional[bool] = Field(None, description="Search for vacation rentals instead of hotels")
    bedrooms: Optional[int] = Field(None, description="Minimum number of bedrooms (vacation rentals only)")
    bathrooms: Optional[int] = Field(None, description="Minimum number of bathrooms (vacation rentals only)")
    property_types: Optional[str] = Field(
        None,
        description=(
            "Property types filter. For Hotels: 12=Beach hotels, 13=Boutique hotels, 14=Hostels, "
            "15=Inns, 16=Motels, 17=Resorts, 18=Spa hotels, 19=Bed and breakfasts, 20=Other, "
            "21=Apartment hotels, 22=Minshuku, 23=Japanese-style business hotels, 24=Ryokan. "
            "For Vacation Rentals: 1=Apartments, 2=Bungalows, 3=Cabins, 4=Chalets, 5=Cottages, "
            "6=Gîtes, 7=Holiday villages, 8=Houses, 9=Houseboats, 10=Villas, 11=Other, "
            "21=Apartment hotels. Comma-separated for multiple (e.g., '17,18,19')"
        )
    )
    amenities: Optional[str] = Field(
        None,
        description=(
            "Amenities filter. For Hotels: 1=Free parking, 3=Parking, 4=Indoor pool, 5=Outdoor pool, "
            "6=Pool, 7=Fitness center, 8=Restaurant, 9=Free breakfast, 10=Spa, 11=Beach access, "
            "12=Child-friendly, 15=Bar, 19=Pet-friendly, 22=Room service, 35=Free Wi-Fi, "
            "40=Air-conditioned, 52=All-inclusive available, 53=Wheelchair accessible, 61=EV charger. "
            "For Vacation Rentals: 2=Hot tub, 4=Air-conditioned, 6=Outdoor grill, 10=Fireplace, "
            "12=Patio or deck, 15=Kitchen, 16=Fitness centre, 18=Cot, 20=Beach access, "
            "21=Child-friendly, 24=Pet-friendly, 29=Free Wi-Fi, 32=Pool. "
            "Comma-separated for multiple (e.g., '35,9,19')"
        )
    )
    max_results: int = Field(5, description="Maximum number of hotel results to return (1-20)")

class HotelSearch:
    def __init__(self):
        self.api_key = os.getenv("SERPAPI_API_KEY")
        self.use_cache = True

    def format_hotel_data_simple(self, hotels):
        """Format hotel data into a readable string."""
        if not hotels:
            return "No hotels found for your search criteria."
        
        output = []
        output.append(f"Found {len(hotels)} hotel(s):\n")
        
        for i, hotel in enumerate(hotels, 1):
            output.append(f"**Hotel {i}: {hotel['name']}**")
            
            # Basic info
            if 'description' in hotel:
                output.append(f"Description: {hotel['description']}")
            
            # Hotel class and rating
            if 'hotel_class' in hotel:
                output.append(f"Hotel Class: {hotel['hotel_class']}")
            if 'overall_rating' in hotel and 'reviews' in hotel:
                output.append(f"Rating: {hotel['overall_rating']}/5 based on {hotel['reviews']} reviews")
            
            # Location rating
            if 'location_rating' in hotel:
                output.append(f"Location Rating: {hotel['location_rating']}/5")
            
            # Pricing
            if 'rate_per_night' in hotel:
                rate = hotel['rate_per_night']
                output.append(f"Rate per night: {rate.get('lowest', 'N/A')}")
                if 'before_taxes_fees' in rate:
                    output.append(f"Before taxes/fees: {rate['before_taxes_fees']}")
            
            if 'total_rate' in hotel:
                total = hotel['total_rate']
                output.append(f"Total rate: {total.get('lowest', 'N/A')}")
            
            # Check-in/out times
            if 'check_in_time' in hotel and 'check_out_time' in hotel:
                output.append(f"Check-in: {hotel['check_in_time']}, Check-out: {hotel['check_out_time']}")
            
            # Amenities
            if 'amenities' in hotel and hotel['amenities']:
                amenities_str = ", ".join(hotel['amenities'][:5])  # Show first 5 amenities
                if len(hotel['amenities']) > 5:
                    amenities_str += f" and {len(hotel['amenities']) - 5} more"
                output.append(f"Amenities: {amenities_str}")
            
            # Nearby places
            if 'nearby_places' in hotel and hotel['nearby_places']:
                output.append("Nearby attractions:")
                for place in hotel['nearby_places'][:3]:  # Show first 3 nearby places
                    place_info = f"- {place['name']}"
                    if 'transportations' in place and place['transportations']:
                        transport = place['transportations'][0]
                        place_info += f" ({transport['duration']} by {transport['type'].lower()})"
                    output.append(place_info)
            
            # Reviews breakdown (show top categories)
            if 'reviews_breakdown' in hotel and hotel['reviews_breakdown']:
                positive_categories = [
                    cat for cat in hotel['reviews_breakdown'] 
                    if cat.get('positive', 0) > cat.get('negative', 0)
                ][:3]  # Top 3 positive categories
                
                if positive_categories:
                    output.append("Guests particularly liked:")
                    for cat in positive_categories:
                        output.append(f"- {cat['name']}: {cat['positive']} positive mentions")
            
            output.append("-" * 50)
            output.append("")  # Blank line for readability
        
        return "\n".join(output)
    
    def _fetch_results(self, params, cache_file):
        """Loads from cache_file if present, otherwise calls SerpAPI and caches."""
        if self.use_cache:
            if os.path.exists(cache_file):
                with open(cache_file, "r") as f:
                    return json.load(f)
        
        search = GoogleSearch(params)
        results = search.get_dict()
        
        if self.use_cache:
            with open(cache_file, "w") as f:
                json.dump(results, f, indent=2)
        return results

    def hotel_search(self, search_params: HotelSearchInput):
        """Perform hotel search using SerpAPI."""
        # Convert Pydantic model to dict if needed
        if isinstance(search_params, HotelSearchInput):
            search_params_dict = search_params.model_dump(exclude_none=True)
        else:
            search_params_dict = {k: v for k, v in search_params.items() if v is not None}
        
        # Add required SerpAPI parameters
        search_params_dict["engine"] = "google_hotels"
        search_params_dict["api_key"] = self.api_key
        search_params_dict["output"] = "json"
        search_params_dict["source"] = "python"
        
        # Remove max_results from params (it's for our processing, not SerpAPI)
        max_results = search_params_dict.pop("max_results", 5)
        
        # Fetch results
        cache_file = f"cache/hotel_search_results.json"
        raw_results = self._fetch_results(search_params_dict, cache_file)
        
        # Extract hotels from results
        hotels = raw_results.get("properties", [])
        
        # Limit results
        max_results = min(max_results, len(hotels))  # SerpAPI max is 20 results
        hotels = hotels[:max_results]
        
        # Format output
        formatted_output = self.format_hotel_data_simple(hotels)
        
        return formatted_output, raw_results


searcher = HotelSearch()

@mcp.tool()
async def hotel_search(search_params: HotelSearchInput):
    """
    Performs a hotel search based on the provided parameters.

    Args:
        search_params (HotelSearchInput): An instance of the HotelSearchInput
            Pydantic model containing all search criteria including location,
            dates, number of guests, and preferences.
    """
    if isinstance(search_params, HotelSearchInput):
        search_params_dict = search_params.model_dump()
    else:
        search_params_dict = search_params

    results = searcher.hotel_search(search_params_dict)
    return results


async def main():
    """Test the hotel search functionality."""
    search_params = {
        "q": "Paris, France",
        "check_in_date": "2025-07-01",
        "check_out_date": "2025-07-05",
        "adults": 2,
        "children": 0,
        "currency": "USD",
        "sort_by": 3,  # Lowest price
        "max_results": 5
    }
    print(await hotel_search(search_params))

if __name__ == "__main__":
    asyncio.run(main())

# mcp.run(transport="streamable-http")