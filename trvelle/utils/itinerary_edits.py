from copy import deepcopy
import hashlib
import json
import re

def hotel_stay_key(params):
    values=[str(params.get('destination') or params.get('q') or '').strip().casefold()]
    values += [str(params.get(k) or '').strip().casefold() for k in ('check_in_date','check_out_date','adults','children','children_ages')]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()[:24]

def replace_hotel(itinerary, hotels, stay_key, uid):
    result=deepcopy(itinerary)
    group=[h for h in hotels if h.get('stay_key')==stay_key]
    chosen=next((h for h in group if h['choose_uid']==uid),None)
    if not chosen:
        raise ValueError('Choose a hotel from the same destination and stay dates.')
    current_uid=result.get('hotel_selections',{}).get(stay_key)
    current=next((h for h in group if h['choose_uid']==current_uid),None) or next((h for h in group if h.get('mentioned_in_plan')),None)
    replaced=False
    for day in result.get('daily_plan',[]):
        for item in day.get('items',[]):
            if not current:continue
            matched=item.get('uid')==current['choose_uid']
            for key in ('title','description','content'):
                text=item.get(key)
                if isinstance(text,str) and current.get('name') and current['name'].casefold() in text.casefold():
                    item[key]=re.sub(re.escape(current['name']),lambda _:chosen['name'],text,flags=re.I)
                    matched=True
            if matched and item.get('card_type') != 'activity':
                item['uid']=uid
                replaced=True
    if not replaced:
        from datetime import date
        days=result.get('daily_plan',[])
        try:
            number=(date.fromisoformat(chosen['check_in_date'])-date.fromisoformat(result['summary']['dates']['start'])).days+1
        except (KeyError,ValueError,TypeError):
            number=days[0].get('day') if days else None
        target=next((day for day in days if day.get('day')==number),None)
        if not target:raise ValueError('The hotel dates do not match a day in this itinerary.')
        target.setdefault('items',[]).append({'item_type':'card','card_type':'hotel','title':chosen['name'],'uid':uid})
    result['hotel_selections']={**result.get('hotel_selections',{}),stay_key:uid}
    return result


def edit_activity(itinerary, day_index, item_index, fields, hotel_names=(), travel_uids=()):
    result=deepcopy(itinerary)
    try:item=result['daily_plan'][day_index]['items'][item_index]
    except (KeyError,IndexError,TypeError):raise ValueError('Activity not found')
    text=' '.join(str(item.get(key,'')) for key in ('title','description')).casefold()
    if item.get('item_type')=='tag' or item.get('card_type') in ('flight','hotel') or item.get('uid') in travel_uids or (not item.get('card_type') and any(name.casefold() in text for name in hotel_names if name)):
        raise ValueError('Use the flight or hotel selection controls to change this item.')
    if any(key in fields and fields[key] != item.get(key) for key in ('title','location')):
        reports=result.get('detail_reports', {})
        for key in list(reports):
            if key.startswith((f'activity:{day_index}:{item_index}',f'activity-place:{day_index}:{item_index}')):reports.pop(key,None)
        for key in ('visitor_information','visitor_details','visitor_information_sources','source_url','cost','place_details','place_name','photos','image_url','image','alternatives'):
            item.pop(key,None)
    item.update({key:value for key,value in fields.items() if key in ('title','description','start_time','end_time','location')})
    if item.get('start_time') and item.get('end_time') and item['end_time'] <= item['start_time']:
        raise ValueError('End time must be after start time.')
    item['item_type']='card'
    item['card_type']='meal' if item.get('card_type') == 'meal' else 'activity'
    return result
