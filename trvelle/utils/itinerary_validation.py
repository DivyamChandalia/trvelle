"""Deterministic checks on saved offers, schedules and numeric totals."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import re


def amount(quote):
    value = quote.get('extracted_lowest') if isinstance(quote, dict) else quote
    if isinstance(value, (int, float)):
        return float(value)
    text = str((quote or {}).get('lowest', '')) if isinstance(quote, dict) else str(quote or '')
    match = re.search(r'[\d,]+(?:\.\d+)?', text)
    return float(match[0].replace(',', '')) if match else None


def validate_trip(trip):
    issues, totals = [], []
    summary = trip.get('summary', {})
    dates = summary.get('dates', {})
    try:
        first, last = date.fromisoformat(str(dates['start'])), date.fromisoformat(str(dates['end']))
    except (KeyError, ValueError):
        return {'status': 'needs_review', 'issues': [{'code': 'trip_dates', 'message': 'Travel dates need to be confirmed.'}], 'priced_total': None}
    if len(trip.get('daily_plan', [])) != (last - first).days + 1:
        issues.append({'code': 'day_count', 'message': 'The daily plan does not cover every travel date.'})
    currency = summary.get('currency') or trip.get('pricing', {}).get('currency') or 'USD'
    selected_flights = [f for f in trip.get('travel_options', {}).get('flights', []) if f.get('selected')]
    requirements = trip.get('requirements', {})
    for flight in selected_flights:
        legs = flight.get('legs', [])
        for index, leg in enumerate(legs):
            flights = leg.get('flights', [])
            if not flights:
                continue
            arrival = str(flights[-1].get('arrival_airport', {}).get('time', '')).split(' ')[0]
            departure = str(flights[0].get('departure_airport', {}).get('time', '')).split(' ')[0]
            if index == 0 and arrival and arrival > first.isoformat():
                issues.append({'code': 'late_arrival', 'message': f'The outbound flight arrives {arrival}; the first planned day is {first.isoformat()}.'})
            if index == len(legs) - 1 and index > 0 and departure != last.isoformat():
                issues.append({'code': 'return_date', 'message': f'The return flight departs {departure}; the trip ends {last.isoformat()}.'})
            leg_currency = leg.get('currency') or currency
        # SerpAPI's final resolved round-trip fare is a party total, counted once.
        if legs and isinstance(legs[-1].get('price'), (int, float)):
            totals.append({'kind': 'flight', 'amount': legs[-1]['price'], 'currency': legs[-1].get('currency') or currency, 'scope': 'party, full journey'})
    for hotel in trip.get('travel_options', {}).get('hotels', []):
        if not hotel.get('selected'):
            continue
        total = amount(hotel.get('total_rate', {}))
        if total is not None:
            totals.append({'kind': 'hotel', 'amount': total, 'currency': hotel.get('currency') or currency, 'scope': 'stay'})
        if not hotel.get('room_type') and not hotel.get('room_description'):
            issues.append({'code': 'room_unverified', 'kind':'hotel', 'uid':hotel.get('choose_uid'), 'field':'Room configuration',
                           'message': f"{hotel.get('name', 'Hotel')}: private room and bed configuration have not been verified."})
        if requirements.get('bed') == 'double' and re.search(r'\btwin\b', hotel.get('name', ''), re.I):
            issues.append({'code': 'bed_mismatch', 'message': 'The selected room is described as twin; a double/queen bed has not been confirmed.'})
        minimum = requirements.get('minimum_rating')
        if minimum and (not hotel.get('overall_rating') or hotel['overall_rating'] < minimum):
            issues.append({'code': 'hotel_rating', 'message': 'The selected hotel does not have a verified rating that meets your preference.'})
        if hotel.get('check_in_date') and hotel['check_in_date'] < first.isoformat() or hotel.get('check_out_date') and hotel['check_out_date'] > last.isoformat():
            issues.append({'code': 'hotel_dates', 'message': f"{hotel.get('name', 'Hotel')} stay dates fall outside the trip."})
    # Count only nights actually spent at the destination, not the outbound flight.
    stay_start, stay_end = first, last
    if selected_flights:
        legs = selected_flights[0].get('legs', [])
        try:
            arrival = date.fromisoformat(str(legs[0]['flights'][-1]['arrival_airport']['time']).split(' ')[0])
            stay_start = max(first, arrival)
            if len(legs) > 1:
                departure = date.fromisoformat(str(legs[-1]['flights'][0]['departure_airport']['time']).split(' ')[0])
                stay_end = min(last, departure)
        except (KeyError, IndexError, ValueError):
            pass
    selected_hotels = [h for h in trip.get('travel_options', {}).get('hotels', []) if h.get('selected')]
    if not requirements.get('accommodation_arranged'):
        for offset in range(max(0, (stay_end - stay_start).days)):
            night = (stay_start + timedelta(days=offset)).isoformat()
            if not any(str(h.get('check_in_date') or '') <= night < str(h.get('check_out_date') or '') for h in selected_hotels):
                issues.append({'code':'hotel_missing', 'kind':'hotel', 'date':night,
                               'message':f'No hotel selected for the night of {night}.'})
    for index, day in enumerate(trip.get('daily_plan', [])):
        current_date = first + timedelta(days=index)
        last_end = None
        for item in day.get('items', []):
            if item.get('card_type') != 'activity':
                continue
            if not item.get('location'):
                issues.append({'code': 'activity_location', 'message': f"Day {day.get('day')}: {item.get('title', 'Activity')} needs a location."})
            start, end = item.get('start_time'), item.get('end_time')
            if not start:
                issues.append({'code': 'activity_time', 'message': f"Day {day.get('day')}: {item.get('title', 'Activity')} needs a suggested time."})
            elif re.fullmatch(r'\d{2}:\d{2}', start or ''):
                if end and re.fullmatch(r'\d{2}:\d{2}', end) and end <= start:
                    issues.append({'code': 'time_order', 'message': f"Day {day.get('day')}: activity end must follow its start."})
                if last_end and start < last_end:
                    issues.append({'code': 'activity_overlap', 'message': f"Day {day.get('day')}: activities overlap."})
                last_end = end or start
                if selected_flights:
                    for leg in selected_flights[0]['legs']:
                        flights = leg.get('flights', [])
                        if not flights:
                            continue
                        arrival = str(flights[-1].get('arrival_airport', {}).get('time', ''))
                        departure = str(flights[0].get('departure_airport', {}).get('time', ''))
                        if index == 0 and arrival.startswith(current_date.isoformat()) and start < arrival[-5:]:
                            issues.append({'code': 'before_arrival', 'message': f"Day 1: {item.get('title', 'Activity')} starts before the flight arrives."})
                        if len(selected_flights[0]['legs']) > 1 and index == len(trip['daily_plan']) - 1 and departure.startswith(current_date.isoformat()) and start > departure[-5:]:
                            issues.append({'code': 'after_departure', 'message': f"Last day: {item.get('title', 'Activity')} is scheduled after departure."})
            if item.get('timezone'):
                try:
                    ZoneInfo(item['timezone'])
                except ZoneInfoNotFoundError:
                    issues.append({'code': 'timezone', 'message': 'An activity timezone is invalid.'})
    priced_total = sum(item['amount'] for item in totals) if totals and all(item['currency'] == currency for item in totals) else None
    budget = summary.get('budget_amount')
    if budget is None and summary.get('budget'):
        # Only a single explicit limit is safe; do not parse an LLM cost breakdown as a budget.
        text = str(summary['budget'])
        if len(re.findall(r'\d[\d,]*(?:\.\d+)?', text)) == 1:
            budget = amount(text)
    if budget and priced_total and priced_total > budget:
        issues.append({'code': 'over_budget', 'message': 'Selected flight and hotel quotes already exceed the trip budget.'})
    elif budget and priced_total and priced_total > budget * .85:
        issues.append({'code': 'unpriced_budget', 'message': f'Only {currency} {budget - priced_total:,.0f} remains for meals, activities and transfers. The full trip budget has not been verified.'})
    return {'status': 'needs_review' if issues else 'checked', 'issues': issues,
        'priced_total': priced_total, 'currency': currency, 'budget_amount': budget,
        'quotes': totals, 'unpriced': ['Activities', 'Meals', 'Local transfers'],
        'notice': 'Search quotes are estimates; availability and final booking prices can change.'}
