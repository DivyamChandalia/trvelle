# Tool Usage Guide for Travel Assistant LLM

This document provides clear guidance on how to use each tool correctly to minimize errors.

## Flight Search Tool

**Tool Name:** `flight_search`

### Required Input Structure:
```json
{
  "search_params": {
    // Either provide flight_legs for multi-city OR simple search parameters
    
    // Option 1: Multi-city search
    "flight_legs": [
      {
        "departure_id": "CDG",
        "arrival_id": "NRT", 
        "date": "2025-06-01",
        "times": "8,18,9,23" // optional: departure_min,departure_max,arrival_min,arrival_max
      }
    ],
    
    // Option 2: One-way/Round-trip search
    "departure_id": "CDG",
    "arrival_id": "NRT", 
    "outbound_date": "2025-06-01",
    "return_date": "2025-06-08", // optional for round-trip
    
    // Common parameters
    "adults": 1,
    "children": 0,
    "travel_class": 1, // 1=Economy, 2=Premium, 3=Business, 4=First
    "currency": "USD",
    "sort_by": 2 // 1=Best, 2=Cheapest, 3=Earliest departure, etc.
  }
}
```

### Common Errors to Avoid:
- ❌ Don't provide both `flight_legs` AND `departure_id/arrival_id`
- ❌ Don't forget `outbound_date` when using simple search
- ❌ Use YYYY-MM-DD format for dates
- ❌ Airport codes should be 3-letter IATA codes

## Hotel Search Tool

**Tool Name:** `hotel_search`

### Required Input Structure:
```json
{
  "search_params": {
    "q": "Paris, France", // Required: location or hotel name
    "check_in_date": "2025-07-01", // Required: YYYY-MM-DD
    "check_out_date": "2025-07-05", // Required: YYYY-MM-DD
    "adults": 2,
    "children": 0,
    "currency": "USD",
    "sort_by": 3, // 3=Lowest price, 8=Highest rating, 13=Most reviewed
    "max_results": 5,
    "rating": 8, // 7=3.5+, 8=4.0+, 9=4.5+
    "hotel_class": "4,5", // Comma-separated: 2,3,4,5 for star ratings
    "free_cancellation": true
  }
}
```

### Common Errors to Avoid:
- ❌ Don't forget required fields: `q`, `check_in_date`, `check_out_date`
- ❌ Use YYYY-MM-DD format for dates
- ❌ Check-out date must be after check-in date

## Itinerary Tool

**Tool Name:** `itinerary_tool`

### Required Input Structure:
```json
{
  "itinerary": {
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
          },
          {
            "item_type": "card",
            "card_type": "flight",
            "title": "Flight to Paris",
            "description": "Depart from JFK",
            "uid": "flight-123"
          }
        ]
      }
    ]
  }
}
```

### Common Errors to Avoid:
- ❌ Don't forget the `itinerary` wrapper object
- ❌ `daily_plan` days must be sequential starting from 1
- ❌ Each item needs either `description` OR proper card structure

## Researcher Agent Tool

**Tool Name:** `researcher_agent`

### Required Input Structure:
```json
{
  "city": "Paris",
  "content": "Explore romantic Paris with visits to iconic landmarks",
  "arrival_datetime": "2025-07-01T10:00:00",
  "departure_datetime": "2025-07-03T18:00:00", 
  "segment_number": 1,
  "adults": 2,
  "children": 0,
  "interests": ["museums", "food", "romance"],
  "information": "Budget: $1500, prefer central location"
}
```

### Common Errors to Avoid:
- ❌ Use ISO format for datetimes: YYYY-MM-DDTHH:MM:SS
- ❌ Don't forget required fields: `city`, `content`, `arrival_datetime`, `departure_datetime`, `segment_number`
- ❌ `segment_number` must be positive integer

## Trip Segment Tool

**Tool Name:** `trip_segment`

### Required Input Structure:
```json
{
  "city": "Rome",
  "content": "Explore ancient Roman history with Colosseum visit and authentic Italian cuisine",
  "arrival_datetime": "2025-07-05T14:30:00",
  "departure_datetime": "2025-07-07T12:00:00",
  "adults": 2,
  "interests": ["history", "food", "architecture"],
  "information": "Budget: $800, prefer walking tours"
}
```

### Common Errors to Avoid:
- ❌ Use ISO format for datetimes: YYYY-MM-DDTHH:MM:SS  
- ❌ Don't forget required fields: `city`, `content`, `arrival_datetime`, `departure_datetime`
- ❌ Make sure departure is after arrival

## General Best Practices

1. **Always validate dates**: Use correct formats (YYYY-MM-DD for dates, ISO for datetimes)
2. **Check required fields**: Every tool has mandatory parameters
3. **Use proper data types**: strings for text, integers for numbers, booleans for true/false
4. **Validate ranges**: passenger counts, ratings, etc. have min/max limits
5. **Be specific**: Use proper airport codes, full location names, clear descriptions

## Error Recovery

If a tool call fails:
1. Check the error message for specific validation failures
2. Verify all required fields are present
3. Confirm data formats match expected patterns
4. Ensure logical constraints (e.g., dates in correct order)
5. Try again with corrected input

## Example Error Messages and Solutions

**Error:** "Field 'outbound_date': field required"
**Solution:** Add `"outbound_date": "2025-06-01"` to flight search

**Error:** "Date must be in YYYY-MM-DD format"  
**Solution:** Change `"2025/06/01"` to `"2025-06-01"`

**Error:** "Must provide either 'flight_legs' for multi-city search OR 'departure_id', 'arrival_id', and 'outbound_date'"
**Solution:** Choose one search type and provide all required fields for that type
