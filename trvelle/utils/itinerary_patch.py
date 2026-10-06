"""Apply bounded itinerary changes while protecting saved travel selections."""
from copy import deepcopy
from typing import Any, Literal
import re
import uuid
from pydantic import BaseModel, Field
from .party_prices import item_identity
from .item_alternatives import minutes
from trvelle.tools.itinerary_tool import PlannedItem, DailyPlan


def update_scope(message):
    text = message.casefold()
    if re.search(r'\b(?:new trip|another trip|plan (?:a|an) (?:\d|trip|vacation|holiday))', text):
        return None
    if re.search(r'\b(?:change|swap|replace|cheaper|different|new|search)\b.{0,30}\b(?:flights?|hotels?)\b|\b(?:flights?|hotels?)\b.{0,30}\b(?:cheaper|change|swap|replace)\b',text):
        return 'inventory'
    if re.search(r'\b\d+\s+rooms?\b', text) and re.search(r'\b(?:use|need|want|change|set)\b', text):
        return 'preferences'
    if re.search(r'\b(?:dates?|destination|depart|departure|return earlier|return later|extend|shorten)\b',text):
        return 'inventory'
    if re.search(r'\b(?:change|add|include|increase|reduce|now|instead)\b.{0,30}\b(?:people|travelers|travellers|adults|children)\b|\b\d+\s+(?:people|travelers|travellers|adults|children)\b',text):
        return 'inventory'
    if re.search(r'\b(?:flight|flights|hotel|hotels|stay|stays)\b', text) and not re.search(r'\b(?:food|breakfast|restaurant|restaurants|eat|meals?)\b', text):
        return 'inventory'
    if re.search(r'\b(?:budget|allowance|rename|title|cost|price|transport|reschedule|earlier|later)\b|\b(?:move|shift|change|set|start|end)\b.{0,40}(?:\btime\b|\d{1,2}:\d{2}|\d{1,2}\s*(?:am|pm)\b)',text):
        return 'general'
    if re.search(r'\b(?:food|foodie|restaurants?|caf[eé]s?|dining|eat|eating|breakfast|lunch|dinner|meals?|vegetarian|vegan)\b', text):
        return 'general' if re.search(r'\b(?:activities|activity|attractions?|sightseeing)\b',text) else 'food'
    if re.search(r'\b(?:activities|activity|attractions?|sightseeing)\b', text):
        return 'activities'
    return 'general'


class PatchOperation(BaseModel):
    action: Literal['append_item', 'replace_item', 'add_alternatives', 'remove_item', 'move_item']
    day_index: int = Field(ge=0)
    item_id: str | None = None
    item: PlannedItem | None = None
    alternatives: list[dict[str, Any]] = Field(default_factory=list, max_length=6)
    target_day_index: int | None = Field(None, ge=0)

class FlightChoice(BaseModel):
    uid: str = Field(min_length=1, max_length=255)
    search_index: int = Field(ge=0)
    option_index: int = Field(ge=0)

class HotelChoice(BaseModel):
    uid: str = Field(min_length=1, max_length=255)
    stay_key: str = Field(min_length=1, max_length=64)
    replace_stay_key: str | None = Field(None, min_length=1, max_length=64)

class DayAction(BaseModel):
    action: Literal['append_day','remove_day']
    day_index: int | None = Field(None, ge=0)
    day: DailyPlan | None = None


class ItineraryPatch(BaseModel):
    operations: list[PatchOperation] = Field(default_factory=list, max_length=60)
    preferences: dict[str, Any] = Field(default_factory=dict)
    summary: str = Field(default='Your itinerary was updated.', max_length=500)
    summary_changes: dict[str, Any] = Field(default_factory=dict)
    trip_name: str | None = Field(None, max_length=200)
    trip_description: str | None = Field(None, max_length=4000)
    day_changes: list[dict[str, Any]] = Field(default_factory=list, max_length=60)
    day_actions: list[DayAction] = Field(default_factory=list, max_length=30, description='Append or remove a destination day when the user changes trip duration; preserve unaffected days and refresh affected flight/stay quotes when needed.')
    flight_uid: str | None = None
    flight_option: FlightChoice | None = Field(None, description='Optional saved flight choice. Dependent return legs are refreshed within this update, then all changes commit together.')
    hotel_choices: list[HotelChoice] = Field(default_factory=list, max_length=30)
    budget_allocations: dict[str, Any] | None = None


def apply_patch(trip, patch, scope):
    result = deepcopy(trip)
    allowed = {'meal'} if scope == 'food' else {'activity'} if scope == 'activities' else set() if scope == 'preferences' else {'activity','meal','transfer','free_time','note'}
    for di, day in enumerate(result.get('daily_plan') or []):
        for ii, item in enumerate(day.get('items') or []):
            item['item_id'] = item_identity(result, di, ii)
    for operation in patch.operations:
        if operation.day_index >= len(result.get('daily_plan') or []):
            raise ValueError('Choose a day in the existing trip.')
        day = result['daily_plan'][operation.day_index]
        items = day.get('items') or []
        target = next((item for item in items if item.get('item_id') == operation.item_id), None)
        generic_meal_break = target and scope == 'food' and target.get('card_type') == 'free_time' and re.search(r'\b(?:breakfast|lunch|dinner|meal|food|eat)\b',f"{target.get('title','')} {target.get('description','')}",re.I)
        if operation.action != 'append_item' and (target is None or (target.get('card_type') not in allowed and not generic_meal_break)):
            raise ValueError('This update cannot change that item. Existing flights, hotels and unrelated activities are protected.')
        if operation.action == 'remove_item':
            items.remove(target)
            day['items'] = items
            continue
        if operation.action == 'move_item':
            if operation.target_day_index is None or operation.target_day_index >= len(result['daily_plan']) or operation.item is None:
                raise ValueError('Choose an existing target day and provide updated item times.')
            copied = deepcopy(operation)
            copied.action = 'append_item'
            copied.day_index = operation.target_day_index
            copied.item = PlannedItem.model_validate({**deepcopy(target),**operation.item.model_dump(mode='json',exclude_unset=True),'item_id':target['item_id']})
            items.remove(target)
            day['items'] = items
            result = apply_patch(result,ItineraryPatch(operations=[copied]),scope)
            continue
        if operation.action == 'add_alternatives':
            candidates = deepcopy(operation.alternatives)
            for candidate in candidates:
                if candidate.get('card_type', target['card_type']) not in allowed:
                    raise ValueError('Alternative belongs to a different item type.')
                candidate['alternative_id'] = candidate.get('alternative_id') or str(uuid.uuid4())
                candidate['card_type'] = 'meal' if generic_meal_break else target['card_type']
                PlannedItem.model_validate(candidate)
            target['alternatives'] = [*(target.get('alternatives') or []), *candidates][:6]
            if generic_meal_break:
                target['card_type'] = 'meal'
            continue
        if operation.item is None or operation.item.card_type not in allowed:
            raise ValueError('Submit a food or activity item belonging to this update.')
        submitted = operation.item.model_dump(mode='json',exclude_unset=True)
        item = {**deepcopy(target or {}),**submitted}
        identity_changed = target and any(key in submitted and submitted[key] != target.get(key) for key in ('title','location','place_name'))
        if identity_changed:
            for key in ('cost','visitor_information','visitor_details','visitor_information_sources','source_url','place_details','photos','image_url','image','alternatives'):
                if key not in submitted:item.pop(key,None)
            if 'description' not in submitted:
                item.pop('description',None)
        item = PlannedItem.model_validate(item).model_dump(mode='json',exclude_none=True)
        if target and not identity_changed and 'cost' not in submitted and 'cost' in target:
            item['cost']=deepcopy(target['cost'])
        item['item_id'] = target['item_id'] if target else item.get('item_id') or str(uuid.uuid4())
        if target and scope in ('food','activities'):
            item['start_time'], item['end_time'] = target.get('start_time'), target.get('end_time')
        start, end = minutes(item.get('start_time')), minutes(item.get('end_time'))
        if start is None or end is None or end <= start:
            raise ValueError('Place this item inside a valid existing daily time window.')
        if item.get('duration_minutes') and item['duration_minutes'] > end - start:
            raise ValueError('The activity does not fit its existing time slot.')
        for other in items:
            if other is target or other.get('card_type') in ('note', 'hotel', 'flight'):
                continue
            other_start, other_end = minutes(other.get('start_time')), minutes(other.get('end_time'))
            if other_start is not None and other_end is not None and start < other_end and end > other_start:
                raise ValueError('This item overlaps the existing day. Attach alternatives or choose a free slot instead.')
        if target:
            items[items.index(target)] = item
        else:
            items.append(item)
            items.sort(key=lambda entry: minutes(entry.get('start_time')) if minutes(entry.get('start_time')) is not None else 1441)
        day['items'] = items
    from trvelle.tools.itinerary_tool import TripRequirements
    preferences = {key:value for key,value in patch.preferences.items() if key in TripRequirements.model_fields}
    if preferences:
        result['requirements'] = TripRequirements.model_validate({**result.get('requirements', {}), **preferences}).model_dump(exclude_none=True)
    if scope in ('general','inventory'):
        if patch.summary_changes:
            from trvelle.tools.itinerary_tool import TripSummary
            result['summary'] = TripSummary.model_validate({**result.get('summary',{}),**patch.summary_changes}).model_dump(mode='json',exclude_none=True)
        if patch.trip_name is not None:
            result['trip_name'] = patch.trip_name
        if patch.trip_description is not None:
            result['trip_description'] = patch.trip_description
        for change in patch.day_changes:
            index=change.get('day_index')
            if not isinstance(index,int) or not 0 <= index < len(result['daily_plan']):
                raise ValueError('Choose a day in the saved itinerary.')
            from trvelle.tools.itinerary_tool import DailyPlan
            result['daily_plan'][index]=DailyPlan.model_validate({**result['daily_plan'][index],**{key:change[key] for key in ('date','destination') if key in change}}).model_dump(mode='json',exclude_none=True)
        for change in patch.day_actions:
            if change.action=='append_day':
                if change.day is None or change.day.date is None:
                    raise ValueError('An added day needs a date and structured daily plan.')
                added=change.day.model_dump(mode='json',exclude_none=True)
                if any(day.get('date')==added['date'] for day in result['daily_plan']):
                    raise ValueError('That destination day is already in the itinerary.')
                result['daily_plan'].append(added)
            else:
                if change.day_index is None or change.day_index >= len(result['daily_plan']):
                    raise ValueError('Choose an existing day to remove.')
                result['daily_plan'].pop(change.day_index)
        if patch.day_actions:
            result['daily_plan'].sort(key=lambda day:day.get('date') or '')
            for index,day in enumerate(result['daily_plan']):day['day']=index+1
        if patch.budget_allocations is not None:
            from .budget import ALLOWANCE_KEYS, number
            if any(number(patch.budget_allocations.get(key)) is None for key in ALLOWANCE_KEYS):
                raise ValueError('Provide nonnegative allowances for every budget category.')
            result['budget_allocations']={key:patch.budget_allocations[key] for key in ALLOWANCE_KEYS}
            result['budget_allocations']['currency']=patch.budget_allocations.get('currency') or result.get('summary',{}).get('currency')
    elif patch.summary_changes or patch.day_changes or patch.day_actions or patch.trip_name or patch.trip_description or patch.budget_allocations:
        raise ValueError('Use a general edit request to change trip-level details.')
    if patch.flight_uid:
        if scope != 'inventory' or not any(f.get('uid')==patch.flight_uid for f in result.get('travel_options',{}).get('flights',[])):
            raise ValueError('Choose a verified flight from this itinerary.')
        result['selected_flight_uid']=patch.flight_uid
        for day in result['daily_plan']:
            for item in day.get('items',[]):
                if item.get('card_type')=='flight':item['uid']=patch.flight_uid
    for choice in patch.hotel_choices:
        choice=choice.model_dump(exclude_none=True)
        if scope != 'inventory':raise ValueError('Hotel choices require a stay edit request.')
        from .itinerary_edits import replace_hotel
        hotels=result.get('travel_options',{}).get('hotels',[])
        old_key=choice.get('replace_stay_key')
        if old_key and old_key != choice['stay_key']:
            previous=next((hotel for hotel in hotels if hotel.get('stay_key')==old_key and hotel.get('selected')),None)
            if previous is None:raise ValueError('Choose an existing selected stay to replace.')
            result['hotel_selections']={**result.get('hotel_selections',{}),choice['stay_key']:previous['choose_uid']}
            hotels=[{**hotel,'stay_key':choice['stay_key']} if hotel.get('choose_uid')==previous['choose_uid'] else hotel for hotel in hotels]
            result=replace_hotel(result,hotels,choice['stay_key'],choice['uid'])
            result['hotel_selections'].pop(old_key,None)
        else:
            result=replace_hotel(result,hotels,choice['stay_key'],choice['uid'])
    travel_uids={flight.get('uid') for flight in result.get('travel_options',{}).get('flights',[])}|{hotel.get('choose_uid') for hotel in result.get('travel_options',{}).get('hotels',[])}
    for day in result.get('daily_plan',[]):
        for item in day.get('items',[]):
            if item.get('card_type') in ('flight','hotel') and item.get('uid') and item['uid'] not in travel_uids:
                raise ValueError('Added travel cards must use verified flight or hotel UIDs.')
    if not any((patch.operations,preferences,patch.summary_changes,patch.day_changes,patch.day_actions,patch.trip_name,patch.trip_description,patch.budget_allocations,patch.flight_uid,patch.flight_option,patch.hotel_choices)):
        raise ValueError('No itinerary changes were submitted.')
    return result
