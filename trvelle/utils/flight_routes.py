"""Keep provider-sourced city names on resolved flight segments."""
from copy import deepcopy


def with_airport_cities(option, airports):
    cities = {}
    for route in airports or []:
        for side in ('departure', 'arrival'):
            for place in route.get(side) or []:
                airport = place.get('airport') or {}
                if airport.get('id') and place.get('city'):
                    cities[airport['id']] = place['city']
    result = deepcopy(option)
    for segment in result.get('flights') or []:
        for side in ('departure_airport', 'arrival_airport'):
            airport = segment.get(side) or {}
            if airport.get('id') in cities:
                airport['city'] = cities[airport['id']]
    return result
