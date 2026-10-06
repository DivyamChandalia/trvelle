"""Stable saved-plan input plus compact changes, including manual UI edits."""
from copy import deepcopy


def context_snapshot(trip):
    def source(value):
        if isinstance(value,dict):
            return {key:source(entry) for key,entry in value.items() if key not in ('price_summary','fits','latest_revision','applied_update_runs','validation','detail_reports')}
        if isinstance(value,list):
            return [source(entry) for entry in value]
        return value
    result = source({key:deepcopy(trip.get(key)) for key in ('itinerary_id','trip_name','trip_description','summary','requirements','daily_plan','hotel_selections','flight_selections','selected_flight_uid','room_allocations','budget_allocations') if key in trip})
    result['selected_hotels'] = source([hotel for hotel in trip.get('travel_options',{}).get('hotels',[]) if hotel.get('selected')])
    result['selected_flights'] = source([flight for flight in trip.get('travel_options',{}).get('flights',[]) if flight.get('selected')])
    return result


def edit_diff(before,after,path=''):
    if before == after:
        return []
    if isinstance(before,dict) and isinstance(after,dict):
        changes=[]
        for key in sorted(set(before)|set(after)):
            pointer=path+'/'+str(key).replace('~','~0').replace('/','~1')
            if key not in after:
                changes.append({'op':'remove','path':pointer})
            elif key not in before:
                changes.append({'op':'add','path':pointer,'value':after[key]})
            else:
                changes.extend(edit_diff(before[key],after[key],pointer))
        return changes
    if isinstance(before,list) and isinstance(after,list):
        changes=[]
        for index in range(min(len(before),len(after))):
            changes.extend(edit_diff(before[index],after[index],path+'/'+str(index)))
        for index in reversed(range(len(after),len(before))):
            changes.append({'op':'remove','path':path+'/'+str(index)})
        for item in after[len(before):]:
            changes.append({'op':'add','path':path+'/-','value':item})
        return changes
    return [{'op':'replace','path':path or '/','value':after}]
