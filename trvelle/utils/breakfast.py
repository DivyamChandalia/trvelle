"""Breakfast is included only when the chosen offer supplies inclusion evidence."""
from urllib.parse import urlparse


def breakfast_for(hotel):
    offer = hotel.get('selected_booking_offer') or {}
    source = offer.get('link') or offer.get('source_url')
    included = offer.get('breakfast_included') is True and urlparse(str(source or '')).scheme in ('https','http')
    advertised = any('breakfast' in str(amenity).casefold() for amenity in hotel.get('amenities') or [])
    return {'status':'included' if included else 'offered' if advertised else 'unknown', 'source_url':source if included else None}


def apply_breakfast_rules(trip):
    hotels = [hotel for hotel in trip.get('travel_options',{}).get('hotels',[]) if hotel.get('selected')]
    for hotel in trip.get('travel_options',{}).get('hotels',[]):
        hotel['breakfast'] = breakfast_for(hotel)
    for day in trip.get('daily_plan') or []:
        for item in day.get('items') or []:
            dining = item.get('dining') or {}
            if item.get('card_type') != 'meal' or dining.get('meal_type') != 'breakfast':
                continue
            named_hotel=any(dining.get('venue_name')==hotel.get('name') for hotel in hotels if hotel.get('name'))
            at_hotel = dining.get('at_hotel') is True or (dining.get('at_hotel') is None and (named_hotel or ('hotel' in str(item.get('title') or '').casefold() and not dining.get('venue_name') and not item.get('place_name'))))
            if not at_hotel:
                continue
            hotel = next((h for h in hotels if h.get('check_in_date','') < day.get('date','') <= h.get('check_out_date','')), None)
            confirmed = hotel and breakfast_for(hotel)['status'] == 'included'
            item['dining'] = {**dining,'at_hotel':True,'breakfast_included':True if confirmed else None}
            if confirmed:
                item['cost'] = {'price':0,'currency':hotel.get('currency') or trip.get('summary',{}).get('currency') or 'INR','scope':'party','status':'quoted','source_url':breakfast_for(hotel)['source_url'],'basis':'Included in the selected hotel rate.'}
            elif (item.get('cost') or {}).get('price') == 0:
                item.pop('cost',None)
    return trip
