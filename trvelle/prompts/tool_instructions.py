"""
Enhanced system instructions for tool usage to minimize errors.
These instructions should be included in LLM prompts to guide proper tool usage.
Separated by agent type to reduce token usage.
"""

SUPERVISOR_TOOL_INSTRUCTIONS = """
## Supervisor Agent Tool Usage Guidelines

You have access to the following tools: `flight_search`, `researcher_agent`, `itinerary_tool`, `tavily_search`

When calling tools, you MUST follow these strict formatting rules to avoid validation errors:

### Flight Search Tool Usage
- **Tool name:** `flight_search`
- **Input wrapper:** Always wrap parameters in `search_params` object
- **Search types:** Choose ONE of the following:
  
  **Multi-city search:**
  ```json
  {
    "search_params": {
      "flight_legs": [
        {"departure_id": "CDG", "arrival_id": "NRT", "date": "2025-07-01"},
        {"departure_id": "NRT", "arrival_id": "LAX", "date": "2025-07-08"}
      ],
      "adults": 2,
      "currency": "USD"
    }
  }
  ```
  
  **One-way/Round-trip search:**
  ```json
  {
    "search_params": {
      "departure_id": "CDG",
      "arrival_id": "NRT",
      "outbound_date": "2025-07-01",
      "return_date": "2025-07-08",
      "adults": 2,
      "currency": "USD",
      "sort_by": 2
    }
  }
  ```

### Researcher Agent Tool Usage
- **Tool name:** `researcher_agent`
- **No wrapper:** Use parameters directly
- **Required fields:** `city`, `content`, `arrival_datetime`, `departure_datetime`, `segment_number`
  
  ```json
  {
    "city": "Paris",
    "content": "Explore romantic Paris with museum visits",
    "arrival_datetime": "2025-07-01T14:30:00",
    "departure_datetime": "2025-07-03T18:00:00",
    "segment_number": 1,
    "adults": 2,
    "interests": ["museums", "food"]
  }
  ```

### Itinerary Tool Usage
- **Tool name:** `itinerary_tool`
- **Input wrapper:** Always wrap in `itinerary` object
- **Required structure:** Must include `trip_name`, `summary`, `daily_plan`
  
  ```json
  {
    "itinerary": {
      "trip_name": "European Adventure",
      "trip_description": "A wonderful European journey",
      "summary": {
        "dates": {"start": "2025-07-01", "end": "2025-07-10"},
        "origin": "New York",
        "travelers": 2,
        "budget": "$5000"
      },
      "daily_plan": [
        {
          "day": 1,
          "items": [
            {"description": "Arrive in Paris"},
            {
              "item_type": "card",
              "card_type": "flight", 
              "title": "Flight to Paris",
              "description": "Direct flight from JFK",
              "uid": "flight-uid-123"
            }
          ]
        }
      ]
    }
  }
  ```

## Critical Formatting Rules for Supervisor

1. **Date Formats:**
   - Flight dates: `YYYY-MM-DD` (e.g., "2025-07-01")
   - Segment datetimes: `YYYY-MM-DDTHH:MM:SS` (e.g., "2025-07-01T14:30:00")

2. **Required Field Validation:**
   - Flight: Either `flight_legs` OR (`departure_id` + `arrival_id` + `outbound_date`)
   - Itinerary: Must have `trip_name` + `summary` + `daily_plan`
   - Researcher Agent: Must have `city` + `content` + both datetime fields + `segment_number`

3. **Input Wrappers:**
   - `flight_search`: Use `search_params` wrapper
   - `itinerary_tool`: Use `itinerary` wrapper
   - `researcher_agent`: No wrapper needed

4. **Common Error Prevention:**
   - ❌ Never mix multi-city and simple flight search parameters
   - ❌ Never forget the wrapper objects for flight/itinerary tools
   - ❌ Never use wrong date formats
   - ❌ Never omit required fields
"""

RESEARCHER_TOOL_INSTRUCTIONS = """
## Researcher Agent Tool Usage Guidelines

You have access to the following tools: `trip_segment`, `hotel_search`, `tavily_search`

When calling tools, you MUST follow these strict formatting rules to avoid validation errors:

### Hotel Search Tool Usage
- **Tool name:** `hotel_search`
- **Input wrapper:** Always wrap parameters in `search_params` object
- **Required fields:** `q`, `check_in_date`, `check_out_date`
  
  ```json
  {
    "search_params": {
      "q": "Paris, France",
      "check_in_date": "2025-07-01",
      "check_out_date": "2025-07-05",
      "adults": 2,
      "children": 0,
      "currency": "USD",
      "sort_by": 3,
      "max_results": 5
    }
  }
  ```

### Trip Segment Tool Usage
- **Tool name:** `trip_segment`
- **No wrapper:** Use parameters directly
- **Required fields:** `city`, `content`, `arrival_datetime`, `departure_datetime`
  
  ```json
  {
    "city": "Rome",
    "content": "Ancient Rome exploration with Colosseum tour",
    "arrival_datetime": "2025-07-05T10:00:00", 
    "departure_datetime": "2025-07-07T16:00:00",
    "adults": 2,
    "interests": ["history", "architecture"]
  }
  ```

## Critical Formatting Rules for Researcher

1. **Date Formats:**
   - Hotel dates: `YYYY-MM-DD` (e.g., "2025-07-01")
   - Segment datetimes: `YYYY-MM-DDTHH:MM:SS` (e.g., "2025-07-01T14:30:00")

2. **Required Field Validation:**
   - Hotel: Must have `q` + `check_in_date` + `check_out_date`
   - Trip Segment: Must have `city` + `content` + both datetime fields

3. **Input Wrappers:**
   - `hotel_search`: Use `search_params` wrapper
   - `trip_segment`: No wrapper needed

4. **Common Error Prevention:**
   - ❌ Never forget the wrapper object for hotel search
   - ❌ Never use wrong date formats
   - ❌ Never omit required fields
"""

COMMON_ERROR_RECOVERY = """
## Error Recovery (Both Agents)

If a tool call fails with validation errors:
1. Check the error message for specific field issues
2. Verify correct input wrapper is used
3. Confirm all required fields are present
4. Validate date/datetime formats
5. Ensure logical constraints (e.g., positive passenger counts)
6. Retry with corrected parameters

## Examples of Common Fixes

**Error:** "Field 'search_params': field required"
**Fix:** Wrap flight/hotel parameters in `search_params` object

**Error:** "Date must be in YYYY-MM-DD format"  
**Fix:** Change `"2025/07/01"` to `"2025-07-01"`

**Error:** "Must provide either 'flight_legs' for multi-city search OR..."
**Fix:** Choose one search type and provide all required fields for that type

**Error:** "Datetime must be in ISO 8601 format"
**Fix:** Change `"2025-07-01 14:30"` to `"2025-07-01T14:30:00"`

Remember: The validation system will catch and explain errors, but following these guidelines prevents them entirely.
"""

# Legacy combined instructions (for backwards compatibility)
TOOL_USAGE_INSTRUCTIONS = f"""
## Tool Usage Guidelines

When calling tools, you MUST follow these strict formatting rules to avoid validation errors:

**IMPORTANT: Tool Availability by Agent Type:**
- **Supervisor Agent Tools:** `flight_search`, `researcher_agent`, `itinerary_tool`, `tavily_search`
- **Researcher Agent Tools:** `trip_segment`, `hotel_search`, `tavily_search`

{SUPERVISOR_TOOL_INSTRUCTIONS}

{RESEARCHER_TOOL_INSTRUCTIONS}

{COMMON_ERROR_RECOVERY}
"""

# Export for use in prompts
__all__ = ["SUPERVISOR_TOOL_INSTRUCTIONS", "RESEARCHER_TOOL_INSTRUCTIONS", "COMMON_ERROR_RECOVERY", "TOOL_USAGE_INSTRUCTIONS"]
