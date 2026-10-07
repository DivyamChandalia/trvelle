"""Optional, server-only Travelpayouts conversion. Booking never depends on it."""
import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import os
from urllib.parse import urlsplit
import httpx
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from trvelle.database.models import SearchCache, SearchAccount, SearchCharge, SearchProviderState
from .flight_booking import safe_booking_url

PROVIDER = 'travelpayouts'
API_URL = 'https://api.travelpayouts.com/links/v1/create'
DEFAULT_HOSTS = 'booking.com,agoda.com,trip.com,expedia.com,hotels.com,hotellook.com,aviasales.com,aviasales.ru,wayaway.io,getyourguide.com,viator.com,klook.com,airalo.com'


def eligible_url(url):
    if not safe_booking_url(url): return False
    host = urlsplit(url).hostname.lower()
    roots = [v.strip().lower() for v in os.getenv('TRAVELPAYOUTS_ALLOWED_HOSTS', DEFAULT_HOSTS).split(',') if v.strip()]
    return any(host == root or host.endswith('.' + root) for root in roots)


def hotel_booking_offers(hotel):
    seen, offers = set(), []
    for index, offer in enumerate([*(hotel.get('prices') or []), *(hotel.get('featured_prices') or [])]):
        if not isinstance(offer, dict): continue
        url = safe_booking_url(offer.get('link'))
        if not url or url in seen: continue
        seen.add(url); offers.append({**offer, 'url': url, 'source_index':index})
    return offers


def retry_time(value, now):
    try:
        seconds = max(1, int(value))
        return now + timedelta(seconds=seconds)
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None: date = date.replace(tzinfo=timezone.utc)
            return max(now + timedelta(seconds=1), date)
        except (ValueError, TypeError, OverflowError): return now + timedelta(seconds=60)


class AffiliateLinks:
    def __init__(self, sessions=None, engine=None, transport=None):
        self.sessions, self.engine, self.transport = sessions, engine, transport

    def convert(self, url, kind='hotel', *, post_data=''):
        # Google booking hand-offs and POST payloads cannot be converted as brand deep links.
        if post_data or not eligible_url(url): return url
        token = os.getenv('TRAVELPAYOUTS_API_TOKEN', '')
        try:
            project, marker = int(os.getenv('TRAVELPAYOUTS_PROJECT_ID', '')), int(os.getenv('TRAVELPAYOUTS_MARKER', ''))
            if not token or project <= 0 or marker <= 0: return url
        except ValueError: return url
        if self.sessions is None:
            from trvelle.database import DBHandler
            self.sessions, self.engine = DBHandler.db_session, DBHandler.engine
        scope = hashlib.sha256(f'{token}:{project}:{marker}'.encode()).hexdigest()
        rate_scope = hashlib.sha256(f'travelpayouts:{marker}'.encode()).hexdigest()
        key = hashlib.sha256(f'{scope}:{kind}:{url}'.encode()).hexdigest()
        lock_id = int.from_bytes(bytes.fromhex(rate_scope)[:8], 'big', signed=True)
        try:
            # Coalesce across workers and enforce one shared partner rate budget.
            with self.engine.connect() as lock:
                lock.execute(text("SET lock_timeout = '750ms'"))
                lock.execute(text('SELECT pg_advisory_lock(:key)'), {'key': lock_id})
                try: return self._locked(url, kind, token, project, marker, scope, rate_scope, key)
                finally: lock.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': lock_id})
        except (SQLAlchemyError, httpx.HTTPError, ValueError, TypeError, KeyError): return url

    def _locked(self, url, kind, token, project, marker, scope, rate_scope, key):
        now = datetime.now(timezone.utc)
        with self.sessions() as db:
            cached = db.get(SearchCache, key)
            if cached and cached.expires_at > now:
                return cached.result.get('partner_url') or url
            state = db.get(SearchProviderState, rate_scope)
            if state and state.cooldown_until and state.cooldown_until > now: return url
            period = now.strftime('%Y-%m-%dT%H:%M')
            account = db.get(SearchAccount, (rate_scope, period))
            limit = min(90, max(1, int(os.getenv('TRAVELPAYOUTS_REQUESTS_PER_MINUTE', '80'))))
            if account and account.used >= limit: return url
            if account is None:
                account = SearchAccount(scope=rate_scope, period=period, provider=PROVIDER, used=0, limit=limit, reset_at=now.replace(second=0, microsecond=0)+timedelta(minutes=1))
                db.add(account)
            account.used += 1
            charge = SearchCharge(provider=PROVIDER, scope=scope, credits=1, cache_key=key, status='reserved')
            db.add(charge); db.commit(); charge_id = charge.charge_id
        success, reason, partner_url, cooldown = False, 'request_failed', None, None
        try:
            with httpx.Client(transport=self.transport, timeout=4, follow_redirects=False) as client:
                response = client.post(API_URL, headers={'X-Access-Token': token}, json={'trs':project, 'marker':marker, 'shorten':True, 'links':[{'url':url, 'sub_id':kind if kind in ('hotel','flight') else 'booking'}]})
            if response.status_code == 429:
                reason, cooldown = 'rate_limited', retry_time(response.headers.get('Retry-After'), now)
            elif response.status_code in (401, 403):
                reason, cooldown = 'access_denied', now + timedelta(hours=1)
            elif response.is_success:
                data = response.json()
                if not isinstance(data, dict): raise ValueError('Invalid affiliate response')
                result = data.get('result', {})
                if data.get('code') == 'success' and isinstance(result, dict):
                    offer = next((v for v in result.get('links', []) if isinstance(v,dict) and v.get('url') == url), {})
                    candidate = safe_booking_url(offer.get('partner_url'))
                    # Short links must be returned by the provider on its tracking domains.
                    if offer.get('code') == 'success' and candidate and (urlsplit(candidate).hostname.endswith('.tp.st') or urlsplit(candidate).hostname in ('tp.media','tp.st')):
                        partner_url, success, reason = candidate, True, 'converted'
                    else: reason = 'brand_unavailable' if offer.get('message') == 'trs is not subscribed for brand' else 'unsupported_link'
                else: reason, cooldown = 'config_rejected', now + timedelta(minutes=5)
            else: cooldown = now + timedelta(seconds=60)
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            cooldown = now + timedelta(seconds=60)
        with self.sessions() as db:
            charge = db.get(SearchCharge, charge_id)
            charge.status = reason
            if cooldown:
                state = db.get(SearchProviderState, rate_scope)
                if state is None:
                    state = SearchProviderState(scope=rate_scope, provider=PROVIDER); db.add(state)
                state.cooldown_until = cooldown
            cache = db.get(SearchCache, key)
            if cache is None:
                cache = SearchCache(cache_key=key, provider=PROVIDER, scope=scope, query={'kind':kind, 'url_hash':hashlib.sha256(url.encode()).hexdigest()}); db.add(cache)
            cache.status, cache.result, cache.fetched_at = 'ready', {'partner_url':partner_url, 'status':reason}, now
            cache.expires_at = now + timedelta(hours=24) if success else now + timedelta(minutes=15)
            # Minute buckets are only useful for recent diagnostics; no unbounded growth.
            db.query(SearchAccount).filter(SearchAccount.provider == PROVIDER, SearchAccount.reset_at < now-timedelta(days=1)).delete()
            db.commit()
        return partner_url or url

    async def resolve(self, url, kind='hotel', *, post_data=''):
        return await asyncio.to_thread(self.convert, url, kind, post_data=post_data)


affiliate_links = AffiliateLinks()
