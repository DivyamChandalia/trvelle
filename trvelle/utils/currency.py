"""Currency preference and sourced display conversion; original quotes stay intact."""
import asyncio
from copy import deepcopy
import re
import time
import httpx

INDIAN_ORIGINS = re.compile(r'\b(india|bangalore|bengaluru|mumbai|delhi|chennai|hyderabad|kolkata|pune|ahmedabad|kochi|jaipur|goa|blr|bom|del|maa|hyd|ccu|pnq|amd|cok|jai|goi|gox)\b', re.I)

def preferred_currency(origin='', fallback='USD'):
    return 'INR' if INDIAN_ORIGINS.search(str(origin)) else fallback

_rates = {}
_rates_lock = asyncio.Lock()
async def exchange_rate(source, target):
    if source == target:
        return 1, None
    key=(source,target)
    async with _rates_lock:
        cached=_rates.get(key)
        if cached and time.time()-cached[0]<86400:
            return cached[1], cached[2]
        async with httpx.AsyncClient(timeout=8) as client:
            response=await client.get(f'https://api.frankfurter.dev/v2/rate/{source.lower()}/{target.lower()}')
            response.raise_for_status()
            data=response.json()
        rate=float(data['rate'])
        if rate<=0:raise ValueError('Invalid exchange rate')
        _rates[key]=(time.time(),rate,data.get('date'))
        return rate,data.get('date')

def currency_from_label(label, fallback):
    for symbol,currency in [('₹','INR'),('€','EUR'),('£','GBP')]:
        if symbol in str(label):return currency
    if '$' in str(label):
        return fallback if fallback in ('USD', 'SGD', 'AUD', 'CAD', 'NZD', 'HKD') else 'USD'
    return fallback

def format_money(value, currency):
    rounded=str(round(value))
    if currency=='INR':
        head,last=rounded[:-3],rounded[-3:]
        groups=[]
        while head:
            groups.insert(0,head[-2:]);head=head[:-2]
        return '₹'+(','.join(groups+[last]) if groups else last)
    return f'{currency} {value:,.0f}'

async def present_currency(payload, target):
    payload=deepcopy(payload)
    quotes={}
    identity = payload.get('itinerary_id') if isinstance(payload, dict) else None
    revision = payload.get('revision', 1) if isinstance(payload, dict) else 1
    frozen = {}
    if identity:
        from trvelle.database import DBHandler
        from trvelle.database.models import FXSnapshot
        import uuid
        with DBHandler.db_session() as db:
            saved = db.get(FXSnapshot, (uuid.UUID(identity), revision, target))
            frozen = saved.rates if saved else {}
    async def convert(value, source):
        if source==target:return value
        try:
            if source in frozen and frozen[source].get('rate'):
                rate, date = frozen[source]['rate'], frozen[source].get('date')
            else:
                rate,date=await exchange_rate(source,target)
            quotes[source]={'rate':rate,'date':date}
            return value*rate
        except (httpx.HTTPError,ValueError,KeyError):
            quotes[source]={'unavailable':True}
            return None
    async def walk(value, source='USD'):
        if isinstance(value,list):
            return [await walk(item,source) for item in value]
        if not isinstance(value,dict):return value
        source=value.get('currency') or value.get('search_parameters',{}).get('currency') or source
        result={}
        for key,item in value.items():
            if key in ('original_quote','pricing'):
                result[key]=item
            elif key == 'budget_breakdown':
                continue  # Recompute from the converted offers below.
            elif key == 'budget_allocations' and isinstance(item, dict):
                from .budget import ALLOWANCE_KEYS
                allocation_source = item.get('currency') or source
                result[key] = {'currency': target, **{
                    name: await convert(item[name], allocation_source) if isinstance(item.get(name), (int, float)) else None
                    for name in ALLOWANCE_KEYS}}
            elif key in ('rate_per_night','total_rate') and isinstance(item,dict):
                original=deepcopy(item)
                amount=item.get('extracted_lowest')
                if amount is None:
                    match=re.search(r'[\d,.]+',str(item.get('lowest','')))
                    amount=float(match[0].replace(',','')) if match else None
                source_rate=currency_from_label(item.get('lowest',''),source)
                converted=await convert(amount,source_rate) if isinstance(amount,(int,float)) else None
                result[key]={**item,'lowest':format_money(converted,target) if converted is not None else 'Price unavailable','extracted_lowest':converted,'currency':target,'original_quote':original}
            else:
                result[key]=await walk(item,source)
        monetary = [key for key in ('price', 'min_price', 'max_price') if isinstance(value.get(key), (int, float))]
        if monetary:
            result['original_quote']={**{key: value[key] for key in monetary}, 'currency':source}
            for key in monetary:
                result[key]=await convert(value[key],source)
            result['currency']=target
        if any(isinstance(value.get(key), dict) for key in ('rate_per_night', 'total_rate')):
            result['currency'] = target
        return result
    result=await walk(payload)
    if isinstance(result,dict):
        result['pricing']={'currency':target,'conversion_rates':quotes,'source':'Frankfurter' if quotes else None}
        summary = result.get('summary', {})
        original_summary = payload.get('summary', {})
        source = original_summary.get('currency') or payload.get('validation', {}).get('currency') or target
        if isinstance(original_summary.get('budget_amount'), (int, float)):
            summary['budget_amount'] = await convert(original_summary['budget_amount'], source)
        summary['currency'] = target
        if 'validation' in result:
            from .itinerary_validation import validate_trip
            report = validate_trip(result)
            if isinstance(payload.get('validation', {}).get('budget_amount'), (int, float)):
                report['budget_amount'] = await convert(payload['validation']['budget_amount'], source)
            result['validation'] = report
        if 'daily_plan' in result:
            from .budget import budget_breakdown
            result['budget_breakdown'] = budget_breakdown(result)
        if identity and quotes:
            with DBHandler.db_session() as db:
                db.merge(FXSnapshot(itinerary_id=uuid.UUID(identity), revision=revision, target=target, rates=quotes))
                db.commit()
    return result
