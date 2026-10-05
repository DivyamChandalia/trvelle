"""Readable display text and a concise, evidence-free itinerary completion message."""
import re
from datetime import date, timedelta


def readable_text(value):
    if not isinstance(value, str) or value.startswith(('https://', 'http://')):
        return value
    # Keep ISO datetimes intact; repair prose such as 'by07:30 for09:50'.
    value = re.sub(r'(?<=[A-Za-z])(?<!\dT)((?:[01]?\d|2[0-3]):[0-5]\d)(?!\d)', r' \1', value)
    return re.sub(r'(?<![\dT])((?:[01]?\d|2[0-3]):[0-5]\d)(?=[A-Za-z])', r'\1 ', value)


def readable_trip(trip):
    def notes(value):
        if isinstance(value, dict):
            return {key: entry if key.endswith(('url', 'uri')) else notes(entry) for key, entry in value.items()}
        if isinstance(value, list):
            return [notes(entry) for entry in value]
        return readable_text(value)
    try:
        start = date.fromisoformat(str(trip.get('summary', {}).get('dates', {}).get('start')))
    except (ValueError, TypeError):
        start = None
    return {**trip, 'daily_plan': [{**day, **({'date': (start + timedelta(days=index)).isoformat()} if start and not day.get('date') else {}), 'items': [
        {**item, **{key: notes(item[key]) for key in ('title', 'description', 'content', 'visitor_information', 'visitor_details') if key in item}}
        for item in day.get('items', [])]} for index, day in enumerate(trip.get('daily_plan', []))]}


def itinerary_chat_text(trip):
    days = trip.get('daily_plan') or []
    origin = str(trip.get('summary', {}).get('origin') or '')
    cities = list(dict.fromkeys(city.strip() for day in days if day.get('destination')
        and not (day.get('items') and all(item.get('card_type') == 'flight' for item in day['items']))
        for city in re.split(r'\s*(?:→|->)\s*', re.split(r'\s+[—–]\s+|\s*\|\s*', str(day['destination']))[0])
        if not re.match(r'^\d{1,2}\s+\w+', city.strip())
        if city.strip() and not (origin and re.search(r'\b' + re.escape(city.strip()) + r'\b', origin, re.I))))
    travelers = trip.get('summary', {}).get('travelers')
    description = f"{len(days)}-day " if days else ''
    title = str(trip.get('trip_name') or '').strip() if days else ''
    description += (title + ' ') if title else (' → '.join(cities) + ' ') if cities else ''
    description += 'draft' if trip.get('planning_status') == 'partial' else 'itinerary'
    party = f" for {travelers} {'traveler' if travelers == 1 else 'travelers'}" if travelers else ''
    ending = 'is saved' if trip.get('planning_status') == 'partial' or not days else 'is ready'
    return f'Your {description}{party} {ending}.'
