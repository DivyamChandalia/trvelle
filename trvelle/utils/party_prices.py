"""A single party-price projection for cards, details and budget children."""
from copy import deepcopy
from datetime import date
import math
import uuid
from .itinerary_validation import amount


def number(value):
    return float(value) if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def item_identity(trip, day_index, item_index):
    item = trip['daily_plan'][day_index]['items'][item_index]
    return item.get('item_id') or str(uuid.uuid5(uuid.NAMESPACE_URL, f"trvelle:{trip.get('itinerary_id', trip.get('trip_name', 'plan'))}:{day_index}:{item_index}"))


def party_price(cost, people):
    cost = deepcopy(cost or {})
    if cost.get('scope') not in ('party', 'per_person'):
        return None
    multiplier = people if cost['scope'] == 'per_person' else 1
    keys = ('price', 'min_price', 'max_price')
    result = {**cost, 'scope': 'party'}
    for key in keys:
        if key in cost:
            result[key] = number(cost[key]) * multiplier if number(cost[key]) is not None else None
    if number(result.get('max_price')) is not None and result.get('status') == 'estimate':
        result['price'] = result['max_price']
    if number(result.get('price')) is None:
        return None
    return result


def item_price(item, people, currency):
    participants = item.get('participants') or people
    cost = item.get('cost') or {}
    cost = {**cost, 'currency': cost.get('currency') or currency}
    return {'total_cost': party_price(cost, participants), 'unit_cost': cost if cost.get('scope') == 'per_person' else None,
            'unit_label': 'per person', 'quantity': participants, 'people': participants, 'assumptions': []}


def hotel_price(hotel, trip):
    people = trip.get('summary', {}).get('travelers') or 1
    requirements = trip.get('requirements') or {}
    planned = (trip.get('room_allocations') or {}).get(hotel.get('stay_key'))
    rooms = planned or hotel.get('requested_rooms') or requirements.get('rooms') or math.ceil(people / 2)
    rooms = max(1, int(rooms))
    assumptions = []
    if not (planned or hotel.get('requested_rooms') or requirements.get('rooms')):
        assumptions.append(f'Assumes {rooms} room(s), up to two travelers per room.')
    try:
        nights = (date.fromisoformat(hotel['check_out_date']) - date.fromisoformat(hotel['check_in_date'])).days
    except (KeyError, ValueError, TypeError):
        nights = None
    quote = hotel.get('total_rate') or {}
    value = number(amount(quote))
    currency = quote.get('currency') or hotel.get('currency') or trip.get('summary', {}).get('currency') or 'INR'
    confirmed_party = hotel.get('quote_scope') == 'party' and hotel.get('quoted_rooms') == rooms
    verified_single = hotel.get('quote_scope') == 'per_room'
    # Legacy search responses do not prove multi-room inventory. Keep their
    # source amount intact and label the planning multiplication explicitly.
    multiplier = 1 if confirmed_party else rooms
    status = 'quoted' if confirmed_party or rooms == 1 else 'estimate'
    if rooms > 1 and not confirmed_party and not verified_single:
        assumptions.append('Assumes the source stay quote covers one room; room capacity and availability are unverified.')
    total = {'price': value * multiplier, 'currency': currency, 'scope': 'party', 'status': status} if value is not None else None
    if total and assumptions:
        total['basis'] = ' '.join(assumptions)
    unit = {'price': value / rooms if confirmed_party and value is not None else value, 'currency': currency, 'status': status, 'scope': 'party'} if value is not None else None
    return {'total_cost': total, 'unit_cost': unit, 'unit_label': 'average per room for the stay' if confirmed_party else 'per room for the stay',
            'quantity': rooms, 'people': people, 'rooms': rooms, 'nights': nights, 'assumptions': assumptions}


def flight_price(flight, people, currency):
    leg = (flight.get('legs') or [{}])[-1]
    value = number(leg.get('price'))
    currency = leg.get('currency') or currency
    total = {'price': value, 'currency': currency, 'scope': 'party', 'status': 'quoted'} if value is not None else None
    unit = {**total, 'price': value / people, 'scope': 'per_person'} if total else None
    return {'total_cost': total, 'unit_cost': unit, 'unit_label': 'average per traveler', 'quantity': people, 'people': people, 'assumptions': []}


def project_prices(trip):
    people = trip.get('summary', {}).get('travelers') or 1
    currency = trip.get('pricing', {}).get('currency') or trip.get('summary', {}).get('currency') or 'INR'
    for day_index, day in enumerate(trip.get('daily_plan') or []):
        for index, item in enumerate(day.get('items') or []):
            item['item_id'] = item_identity(trip, day_index, index)
            item['price_summary'] = item_price(item, people, currency)
            for alternative in item.get('alternatives') or []:
                from .item_alternatives import alternative_fits
                alternative['alternative_id'] = alternative.get('alternative_id') or str(uuid.uuid5(uuid.NAMESPACE_URL,f"{item['item_id']}:{alternative.get('title')}:{alternative.get('location')}"))
                alternative['fits'] = alternative_fits(item, alternative)
                alternative['price_summary'] = item_price(alternative, people, currency)
    for hotel in trip.get('travel_options', {}).get('hotels', []):
        hotel['price_summary'] = hotel_price(hotel, trip)
    for flight in trip.get('travel_options', {}).get('flights', []):
        flight['price_summary'] = flight_price(flight, people, currency)
    return trip
