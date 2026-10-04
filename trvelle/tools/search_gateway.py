"""One paid-search boundary shared by the API, worker and MCP tools.

Postgres advisory locks coalesce identical queries across processes. Reservations
commit before network I/O: a crashed/uncertain request is never silently billed twice.
Only fingerprints, redacted results and normalized queries are persisted.
"""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

import httpx
from sqlalchemy import func, text
from trvelle.database.models import SearchCache, SearchAccount, SearchCharge, SearchProviderState, PlanningRun
from trvelle.utils.secrets import redact_secrets


class SearchBudgetError(RuntimeError):
    pass


class BraveResponseError(SearchBudgetError):
    def __init__(self, status, retry_at):
        self.status, self.retry_at = status, retry_at
        super().__init__(f'Brave search failed (HTTP {status}).' + (f' Retry after {retry_at.isoformat()}.' if retry_at else ' Check access to this search endpoint.'))


DEFAULT_LIMITS = {'serpapi': 2, 'tavily': 2, 'brave': 2}


def brave_run_budget():
    maximum = min(24, max(0, int(os.getenv('BRAVE_RUN_REQUEST_LIMIT', '20'))))
    photos = min(maximum, max(0, int(os.getenv('BRAVE_ITINERARY_PLACE_LIMIT', '8'))))
    return maximum, photos


@dataclass
class SearchContext:
    run_id: uuid.UUID | None = None
    limits: dict = field(default_factory=lambda: dict(DEFAULT_LIMITS))
    used: dict = field(default_factory=dict)
    providers: dict = field(default_factory=dict)
    reserves: dict = field(default_factory=dict)
    media_used: int = 0


active_search: ContextVar[SearchContext | None] = ContextVar('search_budget', default=None)


@contextmanager
def search_context(run_id=None, limits=None, providers=None, reserves=None):
    token = active_search.set(SearchContext(run_id, {**DEFAULT_LIMITS, **(limits or {})}, providers=providers or {}, reserves=reserves or {}))
    try:
        yield
    finally:
        active_search.reset(token)


def normalized_query(provider, params):
    ignored = {'api_key', 'apiKey', 'authorization', 'max_results'} if provider == 'serpapi' else {'api_key', 'authorization', 'x-subscription-token', 'BRAVE_API_KEY'}
    clean = {key: value for key, value in params.items() if key not in ignored and value is not None}
    for key in ('q', 'query'):
        if isinstance(clean.get(key), str):
            value = ' '.join(clean[key].split()).strip()
            # Google operators are case-sensitive; lowercasing OR changes the query.
            parts = re.split(r'(\b(?:OR|AND|NOT)\b)', value) if provider in ('serpapi', 'brave') and key == 'q' else [value]
            clean[key] = ''.join(part if part in ('OR','AND','NOT') else part.casefold() for part in parts)
    # Explicit defaults eliminate accidental duplicate searches without changing filters.
    if provider == 'tavily':
        clean.setdefault('search_depth', 'basic')
        clean.setdefault('max_results', 3)
    if provider == 'brave':
        clean.setdefault('endpoint', 'web')
        if clean['endpoint'] != 'pois':
            clean.setdefault('count', 5)
    return clean


def query_key(provider, params, scope='replay'):
    query = normalized_query(provider, params)
    return hashlib.sha256(json.dumps([provider, scope, query], sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def ttl(provider, params):
    if provider == 'brave' and params.get('endpoint') in ('places', 'pois'):
        return 7 * 3600  # Brave POI identifiers expire after approximately eight hours.
    return 900 if params.get('engine') == 'google_flights' else 3600 if params.get('engine') == 'google_hotels' else 86400


class SearchGateway:
    def __init__(self, session_factory=None, engine=None, transport=None):
        self.sessions, self.engine, self.transport = session_factory, engine, transport

    def database(self):
        if self.sessions is None:
            from trvelle.database import DBHandler
            self.sessions, self.engine = DBHandler.db_session, DBHandler.engine

    def identity(self, provider):
        names = {'serpapi':'SERPAPI_API_KEY', 'tavily':'TAVILY_API_KEY', 'brave':'BRAVE_API_KEY'}
        if provider not in names:
            raise SearchBudgetError('Unsupported search provider.')
        key = os.getenv(names[provider], '')
        if not key:
            raise SearchBudgetError(f'{provider} is not configured.')
        return key, hashlib.sha256(key.encode()).hexdigest()

    def request(self, provider, params):
        if os.getenv('TRVELLE_SEARCH_MODE') == 'replay':
            path = Path(os.getenv('TRVELLE_REPLAY_DIR', 'tests/fixtures/search')) / (query_key(provider, params) + '.json')
            if not path.is_file():
                raise SearchBudgetError('No replay fixture matches this exact search. Live search is disabled.')
            data = json.loads(path.read_text())
            data['_trvelle_search'] = {'mode': 'replay', 'stale': True, 'availability': 'historical fixture'}
            return data
        self.database()
        api_key, scope = self.identity(provider)
        normalized = normalized_query(provider, params)
        cache_key = query_key(provider, params, scope)
        lock_id = int.from_bytes(bytes.fromhex(cache_key)[:8], 'big', signed=True)
        # A session-level lock can survive reservation commits; bounded lock wait.
        with self.engine.connect() as lock:
            lock.execute(text("SET lock_timeout = '45s'"))
            lock.execute(text('SELECT pg_advisory_lock(:key)'), {'key': lock_id})
            try:
                return self._locked_request(provider, normalized, api_key, scope, cache_key)
            finally:
                lock.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': lock_id})

    def _locked_request(self, provider, params, api_key, scope, cache_key):
        now = datetime.now(timezone.utc)
        context = active_search.get()
        credits = 2 if provider == 'tavily' and params.get('search_depth') == 'advanced' else 1
        with self.sessions() as db:
            cached = db.get(SearchCache, cache_key)
            if cached and cached.expires_at > now:
                if cached.status == 'ready':
                    result = deepcopy(cached.result)
                    result['_trvelle_search'] = {'mode': 'cache', 'fetched_at': cached.fetched_at.isoformat(), 'stale': False}
                    return result
                if cached.status == 'uncertain' or cached.status == 'pending':
                    raise SearchBudgetError('A previous search may have reached the provider. Its outcome is unknown; use saved results or wait before retrying.')
                if cached.status == 'failed':
                    raise SearchBudgetError(f'The previous {provider} search failed. Retry after {cached.expires_at.isoformat()}.')
            db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': int.from_bytes(bytes.fromhex(scope)[:8], 'big', signed=True)})
            delay = 0
            if provider == 'brave':
                state = db.get(SearchProviderState, scope)
                if state is None:
                    state = SearchProviderState(scope=scope, provider=provider)
                    db.add(state)
                if state.cooldown_until and state.cooldown_until > now:
                    raise SearchBudgetError(f'Brave is cooling down until {state.cooldown_until.isoformat()}. Cached results remain available.')
                rps = max(.1, min(float(os.getenv('BRAVE_REQUESTS_PER_SECOND', '1')), 20))
                scheduled = max(now, state.next_request_at or now)
                delay = (scheduled - now).total_seconds()
                if delay > 10:
                    raise SearchBudgetError('Brave requests are already queued. Use saved results or retry later.')
                state.next_request_at = scheduled + timedelta(seconds=1 / rps + .05)
            account = db.query(SearchAccount).filter_by(scope=scope, provider=provider).filter(SearchAccount.reset_at > now).order_by(SearchAccount.reset_at.asc()).with_for_update().first()
            if account is None:
                # Reconcile immediately on a new credential; never assume a fresh quota.
                usage = self.provider_usage(provider, api_key)
                reset = usage['reset_at']
                account = SearchAccount(scope=scope, period=reset.isoformat(), provider=provider,
                    used=usage['used'], limit=usage['limit'], reset_at=reset, checked_at=now)
                db.add(account)
                db.flush()
            elif account.checked_at is None or (now - account.checked_at).total_seconds() > 3600:
                usage = self.provider_usage(provider, api_key)
                account.used = max(account.used, usage['used'])
                account.limit, account.checked_at = usage['limit'], now
            if account.used + credits > account.limit:
                raise SearchBudgetError(f'{provider} search budget is exhausted. Resets {account.reset_at.isoformat()}. Saved search results can still be used.')
            if context:
                if context.run_id:
                    # Lock the run as well: independent destination researchers share one budget.
                    run = db.query(PlanningRun).filter_by(run_id=context.run_id).with_for_update().one()
                    used = db.query(func.coalesce(func.sum(SearchCharge.credits), 0)).filter_by(run_id=context.run_id, provider=provider).scalar()
                    maximum = run.request.get('search_limits', {}).get(provider, context.limits.get(provider, 0))
                    reserve = run.request.get('search_reserves', {}).get('brave_places', 0)
                    media_used = db.query(func.coalesce(func.sum(SearchCharge.credits), 0)).join(SearchCache, SearchCharge.cache_key == SearchCache.cache_key).filter(
                        SearchCharge.run_id == context.run_id, SearchCharge.provider == 'brave',
                        SearchCache.query['endpoint'].astext.in_(['places','pois'])).scalar() if provider == 'brave' and reserve else 0
                else:
                    used, maximum = context.used.get(provider, 0), context.limits.get(provider, 0)
                    reserve, media_used = context.reserves.get('brave_places', 0), context.media_used
                if provider == 'brave' and params.get('endpoint', 'web') == 'web':
                    # Research cannot consume the last requests needed to attach
                    # sourced place photos when the itinerary is published.
                    maximum -= max(0, min(reserve, maximum) - media_used)
                if used + credits > maximum:
                    raise SearchBudgetError(f'This task reached its {provider} budget ({maximum}). Finish with saved results; additional searches need an explicit budget extension.')
            # Optional hard ceiling for the entire local acceptance session, across runs.
            cap = os.getenv('TRVELLE_TEST_' + provider.upper() + '_LIMIT')
            if cap:
                start = os.getenv('TRVELLE_TEST_STARTED_AT')
                charges = db.query(func.coalesce(func.sum(SearchCharge.credits), 0)).filter_by(provider=provider, scope=scope)
                if start:
                    charges = charges.filter(SearchCharge.created_at >= datetime.fromisoformat(start))
                if charges.scalar() + credits > int(cap):
                    raise SearchBudgetError('The live acceptance search ceiling is reached; use replay fixtures.')
            account.used += credits
            if context:
                context.used[provider] = context.used.get(provider, 0) + credits
                if provider == 'brave' and params.get('endpoint') in ('places','pois'):
                    context.media_used += credits
            charge = SearchCharge(run_id=context.run_id if context else None, provider=provider, scope=scope, credits=credits, cache_key=cache_key)
            db.add(charge)
            charge_id = charge.charge_id = uuid.uuid4()
            row = SearchCache(cache_key=cache_key, provider=provider, scope=scope, query=params,
                status='pending', fetched_at=now, expires_at=now + timedelta(seconds=ttl(provider, params)))
            db.merge(row)
            db.commit()
        try:
            if delay:
                time.sleep(delay)
            if self.transport:
                result = self.transport(provider, params, api_key)
            else:
                with httpx.Client(timeout=httpx.Timeout(40, connect=10)) as client:
                    if provider == 'serpapi':
                        response = client.get('https://serpapi.com/search.json', params={**params, 'api_key': api_key})
                    elif provider == 'tavily':
                        response = client.post('https://api.tavily.com/search', headers={'Authorization': 'Bearer ' + api_key}, json=params)
                    else:
                        paths = {'web':'web/search', 'places':'local/place_search', 'pois':'local/pois'}
                        endpoint = params.get('endpoint', 'web')
                        if endpoint not in paths:
                            raise SearchBudgetError('Unsupported Brave endpoint.')
                        response = client.get('https://api.search.brave.com/res/v1/' + paths[endpoint],
                            headers={'X-Subscription-Token':api_key, 'Accept':'application/json'},
                            params={k:v for k,v in params.items() if k != 'endpoint'})
                        retry_at = self.brave_headers(scope, response.headers, response.status_code)
                        if response.status_code >= 400:
                            raise BraveResponseError(response.status_code, retry_at)
                    if response.status_code >= 400:
                        raise SearchBudgetError(f'{provider} search failed (HTTP {response.status_code}). Saved results are retained.')
                    result = response.json()
            result = redact_secrets(result)
            if result.get('error'):
                raise SearchBudgetError(f'{provider} could not complete this search. Check dates and filters or use saved results.')
            with self.sessions() as db:
                row = db.get(SearchCache, cache_key)
                row.status, row.result = 'ready', result
                db.get(SearchCharge, charge_id).status = 'completed'
                db.commit()
        except Exception as error:
            with self.sessions() as db:
                row = db.get(SearchCache, cache_key)
                charge = db.get(SearchCharge, charge_id)
                row.status, charge.status = 'uncertain', 'uncertain'
                if isinstance(error, BraveResponseError):
                    # Brave documents non-success responses as unbilled. Count the
                    # attempt, release its reservation, and honor the actual reset.
                    db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key':int.from_bytes(bytes.fromhex(scope)[:8], 'big', signed=True)})
                    account = db.query(SearchAccount).filter_by(scope=scope, provider=provider).filter(SearchAccount.reset_at > now).first()
                    account.used = max(0, account.used - credits)
                    charge.credits, charge.status, row.status = 0, 'failed', 'failed'
                    row.expires_at = error.retry_at or datetime.now(timezone.utc) + timedelta(minutes=5)
                    if context:
                        context.used[provider] = max(0, context.used.get(provider, 0) - credits)
                        if provider == 'brave' and params.get('endpoint') in ('places','pois'):
                            context.media_used = max(0, context.media_used - credits)
                db.commit()
            if isinstance(error, SearchBudgetError):
                raise
            # httpx exceptions can include full credential-bearing request URLs.
            raise SearchBudgetError(f'{provider} search did not finish. The outcome is uncertain; the request will not be silently repeated.') from None
        if directory := os.getenv('TRVELLE_RECORD_SEARCH_DIR'):
            path = Path(directory)
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            fixture = path / (query_key(provider, params) + '.json')
            fixture.write_text(json.dumps(result, ensure_ascii=False))
            fixture.chmod(0o600)
        result = deepcopy(result)
        result['_trvelle_search'] = {'mode': 'live', 'fetched_at': now.isoformat(), 'stale': False}
        return result

    def provider_usage(self, provider, key):
        now = datetime.now(timezone.utc)
        reset = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=timezone.utc)
        if provider == 'brave':
            # Brave reports plan quotas on search responses, not an account API.
            # This is an explicit local safety ceiling, never a claimed plan quota.
            return {'used':0, 'limit':max(0, int(os.getenv('BRAVE_MONTHLY_REQUEST_LIMIT', '100'))), 'reset_at':reset}
        with httpx.Client(timeout=10) as client:
            if provider == 'serpapi':
                response = client.get('https://serpapi.com/account.json', params={'api_key': key})
                if response.status_code != 200:
                    raise SearchBudgetError('SerpAPI quota could not be checked. Search is paused to protect your allowance.')
                data = response.json()
                for name in ('searches_per_month', 'total_searches_left', 'this_month_usage'):
                    if not isinstance(data.get(name), (int, float)):
                        raise SearchBudgetError('SerpAPI returned incomplete quota information.')
                date = data.get('plan_renewal_date')
                if date:
                    try:
                        reset = datetime.fromisoformat(str(date).replace('Z', '+00:00')).replace(tzinfo=timezone.utc)
                    except ValueError:
                        pass
                return {'used': int(data['this_month_usage']), 'limit': int(data['this_month_usage'] + data['total_searches_left']), 'reset_at': reset}
            response = client.get('https://api.tavily.com/usage', headers={'Authorization': 'Bearer ' + key})
            if response.status_code != 200:
                raise SearchBudgetError('Tavily quota could not be checked. Search is paused to protect your allowance.')
            data = response.json().get('account', {})
            used = data.get('plan_usage')
            limit = data.get('plan_limit')
            if not isinstance(used, (int, float)) or not isinstance(limit, (int, float)):
                raise SearchBudgetError('Tavily returned incomplete quota information.')
            return {'used': int(used), 'limit': int(limit), 'reset_at': reset}

    async def arequest(self, provider, params):
        return await asyncio.to_thread(self.request, provider, params)

    def brave_headers(self, scope, headers, status):
        now = datetime.now(timezone.utc)
        def values(name):
            try:
                return [float(value.strip()) for value in headers.get(name, '').split(',') if value.strip()]
            except ValueError:
                return []
        remaining, resets, limits = values('x-ratelimit-remaining'), values('x-ratelimit-reset'), values('x-ratelimit-limit')
        waits = [resets[i] for i, amount in enumerate(remaining) if amount <= 0 and i < len(resets)
                 and (i >= len(limits) or limits[i] > 0)]  # A zero plan limit means unlimited.
        if status == 429 and not waits:
            waits = values('retry-after') or [60]
        wait = max(waits, default=0)
        retry_at = now + timedelta(seconds=max(0, wait) + .1) if wait else None
        with self.sessions() as db:
            db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key':int.from_bytes(bytes.fromhex(scope)[:8], 'big', signed=True)})
            state = db.get(SearchProviderState, scope)
            state.quota = {'limits':limits, 'remaining':remaining, 'reset_seconds':resets, 'observed_at':now.isoformat()}
            if retry_at:
                if wait <= 2 and status < 400:
                    state.next_request_at = max(state.next_request_at or now, retry_at)
                else:
                    state.cooldown_until = max(state.cooldown_until or now, retry_at)
            db.commit()
        return retry_at

    def status(self):
        self.database()
        now = datetime.now(timezone.utc)
        current_scopes = {}
        for provider in ('serpapi', 'tavily', 'brave'):
            try:
                current_scopes[provider] = self.identity(provider)[1]
            except SearchBudgetError:
                pass
        with self.sessions() as db:
            accounts = [row for row in db.query(SearchAccount).filter(SearchAccount.reset_at > now).all()
                        if row.scope == current_scopes.get(row.provider)]
            states = {row.scope:row for row in db.query(SearchProviderState).all()}
            return {'providers': [{'provider': row.provider, 'used': row.used, 'limit': row.limit,
                'remaining': max(0, row.limit - row.used), 'reset_at': row.reset_at.isoformat(),
                'limit_source': 'local ceiling' if row.provider == 'brave' else 'provider account',
                'provider_quota':states[row.scope].quota if row.scope in states else None,
                'cooldown_until':states[row.scope].cooldown_until.isoformat() if row.scope in states and states[row.scope].cooldown_until else None,
                'checked_at': row.checked_at.isoformat() if row.checked_at else None} for row in accounts],
                'requests': [{'provider':provider, 'attempts':db.query(SearchCharge).filter_by(provider=provider).count(),
                    'credits':int(db.query(func.coalesce(func.sum(SearchCharge.credits),0)).filter_by(provider=provider).scalar()),
                    'completed':db.query(SearchCharge).filter_by(provider=provider,status='completed').count(),
                    'failed':db.query(SearchCharge).filter_by(provider=provider,status='failed').count(),
                    'uncertain':db.query(SearchCharge).filter_by(provider=provider,status='uncertain').count()} for provider in ('serpapi','tavily','brave')],
                'cached_queries': db.query(SearchCache).filter_by(status='ready').count()}


gateway = SearchGateway()
