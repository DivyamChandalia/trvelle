"""Destination days run from the outbound arrival to the return departure."""
from datetime import date
from copy import deepcopy
import re
from .party_prices import item_identity


def at_date(airport):
    try:
        return date.fromisoformat(str((airport or {}).get('time') or '')[:10]).isoformat()
    except ValueError:
        return None


def destination_calendar(trip):
    chosen = next((f for f in trip.get('travel_options',{}).get('flights',[]) if f.get('selected')), None)
    if not chosen or not chosen.get('legs'):
        return trip
    legs = chosen['legs']; first = legs[0].get('flights') or []
    if not first:
        return trip
    start = at_date(first[-1].get('arrival_airport'))
    last = legs[-1].get('flights') or []
    round_trip = len(legs) > 1 and last and first[0].get('departure_airport',{}).get('id') == last[-1].get('arrival_airport',{}).get('id')
    end = at_date(last[0].get('departure_airport')) if round_trip else trip.get('summary',{}).get('dates',{}).get('end')
    if not start or not end or end < start:
        return trip
    days = trip.get('daily_plan') or []
    old_positions = {}
    for di, day in enumerate(days):
        for ii,item in enumerate(day.get('items') or []):
            item['item_id'] = item_identity(trip,di,ii)
            old_positions[(str(di),str(ii))] = item['item_id']
    selected_days = [day for day in days if start <= str(day.get('date') or '') <= end]
    if not selected_days:
        return trip
    moved = [deepcopy(item) for day in days if str(day.get('date') or '') < start for item in day.get('items') or [] if item.get('card_type') == 'flight']
    if moved:
        existing = {item.get('uid') for item in selected_days[0].get('items') or [] if item.get('card_type') == 'flight'}
        selected_days[0]['items'] = [item for item in moved if item.get('uid') not in existing] + selected_days[0].get('items',[])
    for index,day in enumerate(selected_days):
        day['day'] = index + 1
    trip['daily_plan'] = selected_days
    positions = {item['item_id']:(str(di),str(ii)) for di,day in enumerate(selected_days) for ii,item in enumerate(day.get('items') or [])}
    reports = {}
    for key,report in (trip.get('detail_reports') or {}).items():
        match = re.match(r'^(activity(?:-place)?):(\d+):(\d+)(.*)$',key)
        if not match:
            reports[key] = report
        elif old_positions.get((match[2],match[3])) in positions:
            di,ii = positions[old_positions[(match[2],match[3])]]
            reports[f'{match[1]}:{di}:{ii}{match[4]}'] = report
    if trip.get('detail_reports'):
        trip['detail_reports'] = reports
    summary = trip.get('summary') or {}
    departure = at_date(first[0].get('departure_airport'))
    home_arrival = at_date(last[-1].get('arrival_airport')) if round_trip else end
    summary['travel_dates'] = {'start':departure,'end':home_arrival}
    summary['dates'] = {'start':start,'end':end}
    for index,leg in enumerate(legs):
        segments = leg.get('flights') or []
        if segments:
            leg['itinerary_date'] = start if index == 0 else at_date(segments[0].get('departure_airport'))
    from .itinerary_validation import validate_trip
    trip['validation'] = validate_trip(trip)
    return trip
