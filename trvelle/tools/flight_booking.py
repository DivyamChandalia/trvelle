"""Booking providers for the saved complete itinerary, without changing its fare."""
from copy import deepcopy
from datetime import datetime,timezone
from urllib.parse import urlsplit
import ipaddress
import httpx
from .detail_lookup import flight_identity


def safe_booking_url(value):
    if not isinstance(value,str):return None
    try:
        url=urlsplit(value)
        if url.scheme!='https' or not url.hostname or url.username or url.password or url.port not in (None,443):return None
        host=url.hostname.casefold()
        if host in ('localhost','localhost.localdomain') or host.endswith('.local'):return None
        try:
            if not ipaddress.ip_address(host).is_global:return None
        except ValueError:pass
        return value
    except ValueError:return None


def selected_parts(raw):
    result=[]
    for part in raw:
        if not isinstance(part,dict):continue
        choices=(part.get('best_flights') or [])+(part.get('other_flights') or [])
        if not choices:continue
        index=part.get('selected_option_index',0)
        if not isinstance(index,int) or not 0<=index<len(choices):raise ValueError('Reopen the selected flight before booking.')
        result.append((part,choices[index]))
    return result


def booking_offers(raw):
    selected=selected_parts(raw)
    if not selected:return []
    item=selected[-1][1]
    offers=[]
    for index,entry in enumerate(item.get('booking_options') or []):
        offer=entry.get('together',entry) if isinstance(entry,dict) else None
        if not isinstance(offer,dict):continue
        request=offer.get('booking_request') or {}
        if not isinstance(request,dict) or not isinstance(request.get('post_data',''),str):continue
        url=safe_booking_url(request.get('url'))
        if not url:continue
        offers.append({**offer,'source_index':index,'booking_request':{'url':url,'post_data':request.get('post_data') or ''},'currency':offer.get('currency') or item.get('booking_currency') or item.get('currency') or selected[-1][0].get('search_parameters',{}).get('currency','INR')})
    return offers


async def fetch_flight_booking(raw,currency,lookup):
    raw=deepcopy(raw);selected=selected_parts(raw)
    if not selected:raise ValueError('The saved flight search is unavailable.')
    part,item=selected[-1]
    if booking_offers(raw):return raw,{'status':'complete','missing':[],'filled':['Booking options'],'summary':'','sources':[],'fetched_at':item.get('booking_fetched_at')}
    report={'status':'unavailable','missing':['Booking options'],'filled':[],'summary':'','sources':[],'fetched_at':datetime.now(timezone.utc).isoformat()}
    token=item.get('booking_token')
    if not token:
        report['provider_notice']='This saved option has no booking token. Open Google Flights to check booking sites, or search a fresh flight option.'
        return raw,report
    allowed=('departure_id','arrival_id','outbound_date','return_date','type','multi_city_json','adults','children','infants_in_seat','infants_on_lap','travel_class','hl','gl')
    params={key:value for key,value in part.get('search_parameters',{}).items() if key in allowed}
    params.update(engine='google_flights',currency=currency,booking_token=token)
    try:response=await lookup.serp(params)
    except (RuntimeError,httpx.HTTPError):
        report['provider_notice']='Booking providers could not be loaded. Please retry or open Google Flights.'
        return raw,report
    incoming=response.get('selected_flights') or []
    if incoming and tuple(flight_identity(leg) for leg in incoming)!=tuple(flight_identity(option) for _,option in selected):
        raise ValueError('The booking results do not match your selected flights. Choose a fresh flight option.')
    item['booking_options']=deepcopy(response.get('booking_options') or [])
    for entry in item['booking_options']:
        if isinstance(entry,dict):
            offer=entry.get('together',entry)
            if isinstance(offer,dict):offer['currency']=currency
    item['booking_currency']=currency
    item['booking_fetched_at']=report['fetched_at']
    link=safe_booking_url(response.get('search_metadata',{}).get('google_flights_url'))
    if link:item['booking_google_flights_url']=link
    if booking_offers(raw):report.update(status='complete',missing=[],filled=['Booking options'])
    else:report['provider_notice']='No direct online booking links were returned for this fare. Check booking sites on Google Flights.'
    return raw,report
