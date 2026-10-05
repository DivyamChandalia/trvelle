"""Party-level budget projection from saved quotes and explicit planning allowances."""
import math

from .itinerary_validation import amount

ALLOWANCE_KEYS = ('activities', 'meals', 'transport', 'buffer')
LABELS = {'flights': 'Flights', 'stays': 'Stays', 'activities': 'Activities',
          'meals': 'Meals', 'transport': 'Transport', 'buffer': 'Buffer'}


def number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def budget_breakdown(trip):
    summary = trip.get('summary') or {}
    currency = trip.get('pricing', {}).get('currency') or summary.get('currency') or 'USD'
    travelers = number(summary.get('travelers')) or 1
    budget = number(summary.get('budget_amount'))
    if budget is None:
        budget = number(trip.get('validation', {}).get('budget_amount'))
    rows = {key: {'key': key, 'label': label, 'quoted': 0, 'estimated': 0,
                  'priced_count': 0, 'unpriced_count': 0} for key, label in LABELS.items()}

    def add(key, price, source, status='quoted'):
        row = rows[key]
        value = number(price)
        if value is None or source != currency:
            row['unpriced_count'] += 1
        else:
            row['quoted' if status == 'quoted' else 'estimated'] += value
            row['priced_count'] += 1

    options = trip.get('travel_options') or {}
    for flight in options.get('flights', []):
        if flight.get('selected'):
            # The final resolved fare covers the party's entire round trip.
            leg = (flight.get('legs') or [{}])[-1]
            add('flights', leg.get('price'), leg.get('currency') or currency)
    seen_stays = set()
    for hotel in options.get('hotels', []):
        if not hotel.get('selected'):
            continue
        identity = hotel.get('stay_key') or hotel.get('choose_uid')
        if identity and identity in seen_stays:
            continue
        seen_stays.add(identity)
        quote = hotel.get('total_rate') or {}
        add('stays', amount(quote), quote.get('currency') or hotel.get('currency') or currency)
    covered = set()
    for day in trip.get('daily_plan', []):
        for item in day.get('items', []):
            kind = item.get('card_type') or item.get('item_type') or 'activity'
            key = {'activity': 'activities', 'meal': 'meals', 'transfer': 'transport'}.get(kind)
            if not key:
                continue
            cost = item.get('cost') or {}
            coverage = cost.get('coverage_key')
            if coverage and coverage in covered:
                continue
            value = number(cost.get('max_price') if cost.get('status') == 'estimate' and cost.get('max_price') is not None else cost.get('price'))
            scope = cost.get('scope')
            if scope not in ('party', 'per_person'):
                value = None
            elif value is not None and scope == 'per_person':
                value *= travelers
            add(key, value, cost.get('currency') or currency, cost.get('status', 'estimate'))
            if coverage and value is not None and (cost.get('currency') or currency) == currency:
                covered.add(coverage)
    quoted = sum(row['quoted'] for row in rows.values())
    estimated = sum(row['estimated'] for row in rows.values())
    known = quoted + estimated
    available = max(0, budget - known) if budget is not None else None
    saved = trip.get('budget_allocations')
    weights = {'activities': .35, 'meals': .35, 'transport': .2, 'buffer': .1}
    for key, row in rows.items():
        row['known'] = round(row['quoted'] + row['estimated'], 2)
        if key in ALLOWANCE_KEYS:
            row['allocation'] = (number(saved.get(key)) if saved.get('currency') == currency else None) if saved else (
                round(row['known'] + available * weights[key], 2) if available is not None else None)
        else:
            row['allocation'] = row['known']
    allowances = [rows[key]['allocation'] for key in ALLOWANCE_KEYS]
    planned = (rows['flights']['known'] + rows['stays']['known'] +
               sum(max(rows[key]['known'], rows[key]['allocation']) for key in ALLOWANCE_KEYS)) if all(value is not None for value in allowances) else None
    return {'currency': currency, 'travelers': travelers, 'budget': budget,
            'quoted_total': round(quoted, 2), 'estimated_total': round(estimated, 2), 'known_total': round(known, 2),
            'remaining_after_known': round(budget - known, 2) if budget is not None else None,
            'planned_total': round(planned, 2) if planned is not None else None,
            'unallocated': round(budget - planned, 2) if budget is not None and planned is not None else None,
            'allocation_source': 'saved' if saved else 'suggested', 'rows': list(rows.values())}
