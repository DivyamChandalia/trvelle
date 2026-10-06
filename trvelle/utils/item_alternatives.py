"""Local item swaps never rewrite a trip or discard their source evidence."""
from copy import deepcopy
import math
import uuid
from .party_prices import item_identity


def minutes(value):
    try:
        hour, minute = str(value).split(':')[:2]
        return int(hour) * 60 + int(minute)
    except (ValueError, TypeError):
        return None


def alternative_fits(item, alternative):
    if alternative.get('card_type', item.get('card_type')) not in ('activity', 'meal'):
        return False
    start, end = minutes(item.get('start_time')), minutes(item.get('end_time'))
    duration = alternative.get('duration_minutes')
    if start is None or end is None or end <= start or not isinstance(duration, (int, float)) or isinstance(duration, bool) or not math.isfinite(duration) or duration <= 0:
        return False
    a = (item.get('place_details') or {}).get('coordinates')
    b = (alternative.get('place_details') or {}).get('coordinates')
    if a and b:
        if len(a) != 2 or len(b) != 2 or not all(isinstance(value,(int,float)) and math.isfinite(value) for value in [*a,*b]):
            return False
        lat1, lon1, lat2, lon2 = map(math.radians, [*a, *b])
        distance = 6371 * 2 * math.asin(min(1, math.sqrt(math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2)))
        if distance > 1:
            return False
        extra = distance / 4.5 * 60
    elif item.get('location') and item['location'] == alternative.get('location') and len(item['location']) > 20:
        extra = 0
    else:
        # A researched local travel estimate must state its geographic basis.
        extra = alternative.get('extra_travel_minutes')
        if not isinstance(extra, (int, float)) or isinstance(extra, bool) or not math.isfinite(extra) or not 0 <= extra <= 15 or not alternative.get('route_fit_basis'):
            return False
    return duration + extra * 2 <= end - start


def replace_item_alternative(trip, item_id, alternative_id):
    result = deepcopy(trip)
    for di, day in enumerate(result.get('daily_plan') or []):
        for ii, item in enumerate(day.get('items') or []):
            if item_identity(result, di, ii) != item_id:
                continue
            selected = next((a for a in item.get('alternatives') or [] if a.get('alternative_id') == alternative_id), None)
            if not selected or not alternative_fits(item, selected):
                raise ValueError('Choose an alternative that fits this time slot and route.')
            from trvelle.tools.itinerary_tool import PlannedItem
            previous = {key: value for key, value in item.items() if key not in ('alternatives', 'price_summary')}
            previous['alternative_id'] = str(uuid.uuid4())
            previous['duration_minutes'] = previous.get('duration_minutes') or minutes(item['end_time']) - minutes(item['start_time'])
            previous.update(extra_travel_minutes=0, route_fit_basis='Previous selection in this same slot.')
            pool = [deepcopy(a) for a in item.get('alternatives') or [] if a.get('alternative_id') != alternative_id]
            updated = PlannedItem.model_validate({**selected, 'item_id': item_id, 'card_type': item.get('card_type'), 'start_time':item['start_time'], 'end_time':item['end_time'], 'alternatives':[previous, *pool][:6]}).model_dump(mode='json', exclude_none=True)
            day['items'][ii] = updated
            reports = result.get('detail_reports') or {}
            for key in list(reports):
                if key.startswith((f'activity:{di}:{ii}', f'activity-place:{di}:{ii}')):
                    reports.pop(key, None)
            return result
    raise ValueError('This item is no longer in the itinerary.')
