"""Brave place matching and provider-sourced activity media. No model-generated photos."""
from copy import deepcopy
from datetime import datetime, timezone
from difflib import SequenceMatcher
import math
import os
import re
import unicodedata
from urllib.parse import urlparse
from langchain_core.tools import tool
from .search_gateway import gateway, SearchBudgetError
from .web_search import search_providers


ALIASES = {'roma':'rome','firenze':'florence','venezia':'venice','bengaluru':'bangalore','mysuru':'mysore','italia':'italy','singapura':'singapore','colosseo':'colosseum','galleria':'gallery','gallerie':'gallery','galleries':'gallery','musei':'museums','vaticani':'vatican','vaticano':'vatican','san':'st','saint':'st','pietro':'peter','piazza':'square','duomo':'cathedral'}
STOP = {'the','a','an','and','in','at','of','to','del','della','dell','degli','di','de','s','visit','visiting','tour','guided','optional','viewpoint','stroll','walk','morning','afternoon','evening'}
COUNTRIES = {'italy':'IT','india':'IN','france':'FR','singapore':'SG','japan':'JP','thailand':'TH','spain':'ES','germany':'DE','uk':'GB','usa':'US','vatican':'VA'}
CITY_COUNTRIES = {'rome':'IT','florence':'IT','venice':'IT','capri':'IT','naples':'IT','milan':'IT','paris':'FR','singapore':'SG','bangalore':'IN','mysore':'IN','delhi':'IN','mumbai':'IN','tokyo':'JP','kyoto':'JP','bangkok':'TH','berlin':'DE','barcelona':'ES','madrid':'ES','london':'GB','vatican':'VA'}
PHRASE_ALIASES = {'foro romano':'roman forum','palatino':'palatine hill','cattedrale di santa maria del fiore':'cathedral','cathedral of santa maria del fiore':'cathedral'}
BRAVE_COUNTRIES = set('AR AU AT BE BR CA CL DK FI FR DE GR HK IN ID IT JP KR MY MX NL NZ NO CN PL PT PH RU SA ZA ES SE CH TW TR GB US ALL'.split())

def tokens(value):
    text = ''.join(c for c in unicodedata.normalize('NFKD', str(value or '').casefold()) if not unicodedata.combining(c))
    for original, translated in PHRASE_ALIASES.items():
        text = re.sub(r'\b'+re.escape(original)+r'\b', translated, text)
    return [ALIASES.get(w,w) for w in re.findall(r'[a-z0-9]+',text) if w not in STOP and not w.isdigit()]

def safe_url(value):
    if not isinstance(value,str): return None
    try:
        url=urlparse(value)
        return value if url.scheme in ('http','https') and url.hostname and not url.username and not url.password else None
    except ValueError:
        return None

def address_of(place):
    address=place.get('postal_address') or {}
    return address.get('displayAddress') or ', '.join(str(address.get(k) or '') for k in ('streetAddress','addressLocality','addressRegion','country') if address.get(k))

def country_of(location):
    words=tokens(location)
    return next((code for word,code in COUNTRIES.items() if word in words),None) or next((code for city,code in CITY_COUNTRIES.items() if city in words),None)

def match_place(name, location, candidates, destination='', coordinates=None, source_url=None):
    """Require both attraction identity and geographic evidence; reject ambiguous ties."""
    # A combined visit can use a photo of its first confidently matched attraction.
    # Try each explicit name independently rather than lowering identity thresholds.
    parts = [part.strip() for part in re.split(r'\s+(?:and|&)\s+|\s*/\s*', name, flags=re.I) if part.strip()]
    if len(parts)>1:
        for part in parts:
            matched = match_place(part, location, candidates, destination, coordinates, source_url)
            if matched: return matched
        return None
    expected=set(tokens(name)); location_words=set(tokens(location))
    # The activity address is more specific than a day heading (Rome → Florence),
    # and Vatican City must not be rejected merely because the day is based in Rome.
    cities = location_words & CITY_COUNTRIES.keys()
    all_area=cities or (set(tokens(destination)) & CITY_COUNTRIES.keys()) or set(tokens(destination or location))
    area=all_area-set(COUNTRIES)-{code.casefold() for code in COUNTRIES.values()}
    area=area or all_area
    if not expected or not area: return None
    expected_country=country_of(location) or country_of(destination)
    trusted_domain=(urlparse(safe_url(source_url) or '').hostname or '').removeprefix('www.')
    matches=[]
    for place in candidates:
        actual=set(tokens(place.get('title') or place.get('name')))
        # City suffixes in venue names do not change attraction identity.
        actual-=all_area; expected_name=expected-all_area or expected
        if not actual: continue
        overlap=2*len(expected_name & actual)/(len(expected_name)+len(actual))
        similarity=SequenceMatcher(None,' '.join(tokens(name)),' '.join(tokens(place.get('title') or place.get('name')))).ratio()
        name_score=max(overlap,similarity)
        if name_score < .65 or not expected_name & actual: continue
        address=place.get('postal_address') or {}
        country=str(address.get('country') or '').upper()
        if expected_country and country and country not in (expected_country, next((n.upper() for n,c in COUNTRIES.items() if c==expected_country),'')):
            continue
        geo=set(tokens(address_of(place)))
        if not area & geo: continue
        if coordinates and place.get('coordinates'):
            try:
                lat,lon=coordinates; plat,plon=place['coordinates']
                km=6371*2*math.asin(min(1,math.sqrt(math.sin(math.radians(plat-lat)/2)**2 + math.cos(math.radians(lat))*math.cos(math.radians(plat))*math.sin(math.radians(plon-lon)/2)**2)))
                if km>40: continue
            except (ValueError,TypeError): continue
        score=.8*name_score+.2*min(1,len(area & geo)/len(area))
        domain=(urlparse(safe_url(place.get('url')) or '').hostname or '').removeprefix('www.')
        if trusted_domain and (domain==trusted_domain or domain.endswith('.'+trusted_domain)):
            score+=.12
        matches.append((score,place))
    matches.sort(key=lambda entry:entry[0],reverse=True)
    if not matches or (len(matches)>1 and matches[0][0]-matches[1][0]<.04 and address_of(matches[0][1])!=address_of(matches[1][1])):
        return None
    return matches[0][1],round(min(1,matches[0][0]),3)

def photos_of(place):
    photos=[]; seen=set()
    for picture in [place.get('thumbnail'), *((place.get('pictures') or {}).get('results') or [])]:
        if not isinstance(picture,dict) or picture.get('logo'): continue
        thumb=picture.get('thumbnail') or picture
        if not isinstance(thumb,dict) or thumb.get('logo'): continue
        url=safe_url(thumb.get('original') or thumb.get('src') or picture.get('image_url'))
        if not url or url in seen: continue
        seen.add(url)
        photos.append({'url':url, 'thumbnail':safe_url(thumb.get('src')) or url,
                       'source_url':safe_url(picture.get('url') or thumb.get('original') or place.get('provider_url') or place.get('url')),
                       'alt':thumb.get('alt') or place.get('title') or place.get('name')})
    return photos[:8]

def normalize_place(place, score, fetched_at=None):
    rating=place.get('rating') or {}; contact=place.get('contact') or {}
    coords=place.get('coordinates')
    return {k:v for k,v in {
        'provider':'brave', 'name':place.get('title') or place.get('name'), 'address':address_of(place),
        'coordinates':coords if isinstance(coords,list) and len(coords)==2 else None,
        'rating':rating.get('ratingValue'), 'rating_scale':rating.get('bestRating'), 'reviews':rating.get('reviewCount'),
        'opening_hours':place.get('opening_hours'), 'phone':contact.get('telephone') or contact.get('phone'),
        'website':safe_url(place.get('url')), 'source_url':safe_url(place.get('provider_url') or place.get('url')),
        'categories':place.get('categories'), 'timezone':place.get('timezone'), 'photos':photos_of(place),
        'price_range':place.get('price_range'), 'cuisine':place.get('cuisine'),
        'match_score':score,'fetched_at':fetched_at or datetime.now(timezone.utc).isoformat()
    }.items() if v is not None and v != '' and v != [] and v != {}}

async def find_place(name, location, *, destination='', photos=False, coordinates=None, source_url=None):
    if not name or not location: return None
    destination=re.split(r'\s+[—–]\s+',destination,1)[0].strip()
    params={'endpoint':'places','q':name,'location':destination or location,'count':5,'safesearch':'moderate'}
    if coordinates:
        params.update(latitude=coordinates[0],longitude=coordinates[1]);params.pop('location',None)
    country=country_of(location+' '+destination)
    params['country']=country if country in BRAVE_COUNTRIES else 'ALL'
    raw=await gateway.arequest('brave',params)
    matched=match_place(name,location,raw.get('results') or [],destination,coordinates,source_url)
    if not matched: return None
    place,score=matched
    if photos and not photos_of(place) and place.get('id'):
        # Resolve IDs immediately from a fresh (<8h) place-search cache. Never
        # persist these temporary IDs in itineraries or use old saved identifiers.
        try:
            details=await gateway.arequest('brave',{'endpoint':'pois','ids':[place['id']]})
            candidates=details.get('results') or []
            fresh=next((p for p in candidates if p.get('id')==place['id']),None)
            if fresh and match_place(name,location,[fresh],destination,coordinates,source_url):
                place={**place,**fresh}
        except SearchBudgetError:
            pass  # A missing photo must not discard a verified place match.
    return normalize_place(place,score,(raw.get('_trvelle_search') or {}).get('fetched_at'))

async def enrich_activity(item, *, destination='', photos=True):
    from trvelle.utils.travel_text import clean_destination
    destination = clean_destination(destination)
    item=deepcopy(item)
    if item.get('place_details') and item['place_details'].get('provider')=='brave' and (item.get('image_url') or not photos):
        return item,False
    place=await find_place(item.get('place_name') or item.get('title') or item.get('name'),item.get('location') or destination,destination=destination,photos=photos,source_url=item.get('source_url'))
    if not place: return item,False
    item['place_details']=place
    if place.get('photos'):
        item['photos']=place['photos'];item['image_url']=place['photos'][0]['thumbnail']
    return item,True

async def enrich_itinerary(itinerary):
    result=deepcopy(itinerary)
    if search_providers()['places']!='brave': return result
    maximum=max(0,min(int(os.getenv('BRAVE_ITINERARY_PLACE_LIMIT','8')),12));attempted=0
    matches={}
    for day in result.get('daily_plan',[]):
        for index,item in enumerate(day.get('items',[])):
            if item.get('card_type') not in ('activity', 'meal') or item.get('item_type')=='tag': continue
            if item.get('card_type') == 'meal' and not (item.get('place_name') or (item.get('dining') or {}).get('venue_name')): continue
            if not item.get('title') or not (item.get('location') or day.get('destination')): continue
            # Generic stroll/rest descriptions have no single place to photograph.
            name=item.get('place_name') or re.split(r'\s+(?:and|&)\s+|\s*/\s*',item['title'],maxsplit=1,flags=re.I)[0]
            if not item.get('place_name') and re.search(r'\b(?:stroll|orientation|neighbou?rhood|riverbanks?|rest)\b',name,re.I): continue
            if (item.get('place_details') or {}).get('provider')=='brave' and item.get('image_url'): continue
            identity=(tuple(tokens(item.get('place_name') or item['title'])),tuple(tokens(item.get('location') or day.get('destination'))))
            if identity in matches:
                media=matches[identity]
                day['items'][index]={**item,**deepcopy(media)}
                continue
            if attempted>=maximum: continue
            attempted+=1
            try:
                enriched,_=await enrich_activity(item,destination=day.get('destination') or '',photos=True)
                day['items'][index]=enriched
                matches[identity]={key:enriched[key] for key in ('place_details','photos','image_url') if key in enriched}
            except SearchBudgetError:
                continue
    result['search_providers']=search_providers()
    return result

@tool
async def brave_place_search(name: str, location: str, fetch_photos: bool = False, source_url: str | None = None) -> dict:
    """Match an exact attraction or venue by its name and destination. Returns only geographically matching, sourced place details and available photos. Extra POI photos are fetched only when requested and no thumbnail exists."""
    import json
    place=await find_place(name,location,photos=fetch_photos,source_url=source_url)
    raw={'place':place,'provider':'brave'}
    return {'result':json.dumps(raw,ensure_ascii=False),'raw':raw}
