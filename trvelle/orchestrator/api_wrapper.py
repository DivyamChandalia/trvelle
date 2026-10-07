"""Authenticated HTTP API used by the Next.js server-side proxy."""
import asyncio
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from contextlib import asynccontextmanager, AsyncExitStack
from fastapi import FastAPI, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from .streaming import with_keepalive
from .chat_runs import ChatRuns
from pydantic import BaseModel, Field
from sqlalchemy import text
from trvelle.utils import load_environment

load_environment()
from trvelle.database import DBHandler

db_handler = DBHandler()
orchestrator = None
chat_locks = {}
chat_runs = ChatRuns()

@asynccontextmanager
async def lifespan(app):
    yield
    await chat_runs.close()
    if orchestrator is not None:
        await orchestrator.shutdown()
    from . import personal_models
    if personal_models.accounts is not None:
        await personal_models.accounts.close()

app = FastAPI(title="Trvelle", version="0.1.0", lifespan=lifespan)

@app.exception_handler(RequestValidationError)
async def private_validation_error(request,error):
    # FastAPI's default validation payload echoes input, including API keys/codes.
    return JSONResponse(status_code=422,content={'detail':[{'loc':list(item['loc']),'msg':item['msg'],'type':item['type']} for item in error.errors()]})


def stop_connection_runs(owner,provider):
    from .run_store import store,RunConflict
    from trvelle.database.models import PlanningRun
    with db_handler.db_session() as db:
        runs=db.query(PlanningRun).filter_by(user_id=owner).filter(PlanningRun.status.in_(['queued','running'])).all()
        identifiers=[run.run_id for run in runs if any(choice and choice.get('provider')==provider for choice in [*(run.request.get('choices') or {}).values(),*(run.checkpoint.get('models') or {}).values()])]
    for identifier in identifiers:
        try:store.control(owner,identifier,'stop')
        except (LookupError,RunConflict):pass


async def authorize(x_backend_token: str = Header(default="")):
    secret = os.getenv("BACKEND_API_TOKEN", "")
    if not secret or not hmac.compare_digest(x_backend_token, secret):
        raise HTTPException(401, "Backend authentication required")

@app.get('/search_configuration', dependencies=[Depends(authorize)])
async def search_configuration(user_id: uuid.UUID = Header()):
    from trvelle.tools.search_gateway import gateway, brave_run_budget
    from trvelle.tools.web_search import search_providers
    return {'defaults':search_providers(),
            'configured':{name:bool(os.getenv(key)) for name,key in {'brave':'BRAVE_API_KEY','tavily':'TAVILY_API_KEY','serpapi':'SERPAPI_API_KEY'}.items()},
            'brave_limits':{'monthly':int(os.getenv('BRAVE_MONTHLY_REQUEST_LIMIT','100')), 'run':brave_run_budget()[0], 'reserved_for_places':brave_run_budget()[1], 'rps':float(os.getenv('BRAVE_REQUESTS_PER_SECOND','1'))},
            'usage':gateway.status()}

def missing_keys():
    missing = [name for name in ("SERPAPI_API_KEY", "TAVILY_API_KEY") if not os.getenv(name)]
    if not (os.getenv("GOOGLE_API_KEY") or os.getenv("OPENROUTER_API_KEY")):
        missing.append("GOOGLE_API_KEY or OPENROUTER_API_KEY")
    return missing

def get_orchestrator():
    global orchestrator
    missing = [name for name in ("SERPAPI_API_KEY", "TAVILY_API_KEY") if not os.getenv(name)]
    if missing:
        raise HTTPException(503, "Configure backend environment: " + ", ".join(missing))
    if orchestrator is None:
        from trvelle.orchestrator.client import Orchestrator
        orchestrator = Orchestrator()
    return orchestrator

@app.get("/health")
async def health():
    database = False
    try:
        with db_handler.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        database = True
    except Exception:
        pass
    from pathlib import Path
    import time
    heartbeat = Path(os.getenv('TRVELLE_WORKER_HEARTBEAT', '.runtime/worker-heartbeat.json'))
    worker = heartbeat.exists() and time.time() - heartbeat.stat().st_mtime < 20
    return {"status": "ok" if database and worker else "degraded", "database": database, 'worker': worker,
            "ai_ready": not missing_keys(), "missing_configuration": missing_keys(),
            "model": os.getenv("GOOGLE_MODEL", "gemini-3.8-flash")}

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    stream: bool = True
    replace_message_id: uuid.UUID | None = None
    resume: bool = False
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")

def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

async def model_owner(user_id: uuid.UUID = Header()):
    from .personal_models import active_owner, active_choices, get_accounts
    token = active_owner.set(str(user_id))
    snapshot = active_choices.set(get_accounts().load(user_id))
    try:
        yield
    finally:
        active_choices.reset(snapshot)
        active_owner.reset(token)

async def detail_budget():
    from trvelle.tools.search_gateway import search_context
    with search_context():
        yield

from .run_api import router as runs_router
app.include_router(runs_router)

class GuestMigration(BaseModel):
    guest_id: uuid.UUID


def check_guest_planning(guest_id, destination=False):
    from trvelle.database.models import PlanningRun
    from .personal_models import get_accounts
    from .model_accounts import AccountError
    with db_handler.db_session() as db:
        if db.query(PlanningRun).filter_by(user_id=guest_id).filter(PlanningRun.status.in_(['queued', 'running'])).first():
            raise HTTPException(409, 'Stop account planning before linking this guest. Your chats are retained.' if destination else 'Stop guest planning before linking this account. Your chats are retained.')
    try:
        get_accounts().check_link_ready(guest_id)
    except AccountError as error:
        raise HTTPException(409, str(error)) from None


@app.post('/guest_migrate/check', dependencies=[Depends(authorize)])
async def check_guest_migration(body: GuestMigration):
    check_guest_planning(body.guest_id)
    return {'ready': True}


@app.post('/guest_migrate', dependencies=[Depends(authorize)])
async def migrate_guest(body: GuestMigration, user_id: uuid.UUID = Header()):
    from trvelle.database.models import User, ChatSession, Message, PlanningRun
    from .personal_models import get_accounts
    from .model_accounts import AccountError
    if body.guest_id == user_id:
        return {'migrated': True}
    accounts = get_accounts()
    async with accounts.migration_locks.setdefault(str(body.guest_id), asyncio.Lock()), AsyncExitStack() as locks:
        for owner in sorted((str(body.guest_id), str(user_id))):
            await locks.enter_async_context(accounts.locks.setdefault(owner, asyncio.Lock()))
        check_guest_planning(body.guest_id)
        check_guest_planning(user_id, destination=True)
        with db_handler.db_session() as db:
            db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:source))"), {'source': str(body.guest_id)})
            if db.query(PlanningRun).filter_by(user_id=body.guest_id).filter(PlanningRun.status.in_(['queued', 'running'])).first():
                raise HTTPException(409, 'Stop guest planning before linking this account. Your chats are retained.')
            try:
                # Retain the source until the ownership transaction commits.
                accounts.copy_credentials(body.guest_id, user_id)
            except AccountError as error:
                raise HTTPException(409, str(error)) from None
            if db.get(User, body.guest_id) is not None:
                if db.get(User, user_id) is None:
                    db.add(User(user_id=user_id))
                    db.flush()
                db.query(ChatSession).filter_by(user_id=body.guest_id).update({'user_id': user_id})
                db.query(Message).filter_by(user_id=body.guest_id).update({'user_id': user_id})
                db.query(PlanningRun).filter_by(user_id=body.guest_id).update({'user_id': user_id})
            db.commit()
        # Safe after interruption, including guests who have only credentials.
        accounts.finish_credential_move(body.guest_id)
    return {'migrated': True}

@app.get("/chat_history", dependencies=[Depends(authorize)])
async def history(user_id: uuid.UUID = Header(), chat_id: uuid.UUID = Query()):
    messages = db_handler.get_filtered_chat_history(user_id, chat_id)
    return {"messages": messages, "user_id": str(user_id), "chat_id": str(chat_id), "total_messages": len(messages)}

class ChatVersionRequest(BaseModel):
    message_id: uuid.UUID


@app.post('/chat_version', dependencies=[Depends(authorize)])
async def chat_version(body: ChatVersionRequest, user_id: uuid.UUID = Header(), chat_id: uuid.UUID = Query()):
    try:
        run_id = db_handler.select_chat_version(user_id, chat_id, body.message_id)
    except LookupError:
        raise HTTPException(404, 'Message version not found') from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    from .run_store import store
    run = store.snapshot(user_id, uuid.UUID(run_id)) if run_id else None
    return {'messages':db_handler.get_filtered_chat_history(user_id, chat_id), 'run':run}

@app.get("/list_chats", dependencies=[Depends(authorize)])
async def chats(user_id: uuid.UUID = Header()):
    result = db_handler.get_user_chats(user_id)
    return {"chats": result, "user_id": str(user_id), "total_chats": len(result)}

@app.get("/tool_call", dependencies=[Depends(authorize)])
async def tool_result(user_id: uuid.UUID = Header(), chat_id: uuid.UUID = Query(), tool_call_id: str = Header(), display_currency: str | None = Query(default=None, pattern=r"^[A-Z]{3}$"), revision: int | None = Query(None, ge=1)):
    try:
        identifier = uuid.UUID(tool_call_id)
    except ValueError:
        result = db_handler.get_tool_response(user_id, chat_id, tool_call_id)
    else:
        result = db_handler.get_itinerary(user_id, chat_id, identifier, revision)
    if not result or "error" in result:
        raise HTTPException(404, "Travel item not found")
    from trvelle.utils.currency import preferred_currency, present_currency
    origin = result.get("summary", {}).get("origin", "") if isinstance(result, dict) else ""
    return await present_currency(result, display_currency or preferred_currency(origin))

@app.get('/itinerary_versions', dependencies=[Depends(authorize)])
async def itinerary_versions(user_id: uuid.UUID = Header(), chat_id: uuid.UUID = Query(), itinerary_id: uuid.UUID = Query()):
    if 'error' in db_handler.get_itinerary(user_id, chat_id, itinerary_id):
        raise HTTPException(404, 'Itinerary not found')
    from trvelle.database.models import ItineraryRevision
    with db_handler.db_session() as db:
        rows = db.query(ItineraryRevision).filter_by(chat_id=chat_id, itinerary_id=itinerary_id).order_by(ItineraryRevision.revision.desc()).all()
        return {'versions': [{'revision': row.revision, 'reason': row.reason, 'created_at': row.created_at.isoformat()} for row in rows]}

@app.delete("/chat", dependencies=[Depends(authorize)])
async def delete_chat(user_id: uuid.UUID = Header(), chat_id: uuid.UUID = Query()):
    from .run_store import store
    try:
        current = store.snapshot(user_id, chat_id=chat_id)
        if current['status'] in ('queued', 'running'):
            store.control(user_id, uuid.UUID(current['run_id']), 'stop')
            for _ in range(8):
                await asyncio.sleep(.25)
                if store.snapshot(user_id, uuid.UUID(current['run_id']))['status'] not in ('queued', 'running'):
                    break
            else:
                raise HTTPException(409, 'Planning is stopping. Delete this chat again in a moment.')
    except LookupError:
        pass
    await chat_runs.stop(user_id, chat_id)
    lock = chat_locks.setdefault((user_id, chat_id), asyncio.Lock())
    async with lock:
        result = db_handler.delete_chat(user_id, chat_id)
        if "error" in result:
            raise HTTPException(404, "Chat not found")
        if orchestrator is not None:
            prefix = f"{user_id}:{chat_id}"
            for key in orchestrator.chat_manager.get_all_chat_keys_with_access_times():
                if key == prefix or key.startswith(prefix + "#"):
                    orchestrator.chat_manager.remove_chat(key)
    return result


@app.get("/models", dependencies=[Depends(authorize)])
async def model_status():
    return get_orchestrator().model_router.status()


class ModelKeyRequest(BaseModel):
    provider: str = Field(pattern=r"^(openai|anthropic|google|openrouter)$")
    key: str = Field(default="", max_length=2048, repr=False)

class ModelChoice(BaseModel):
    provider: str = Field(pattern=r"^(openai|anthropic|google|openrouter|chatgpt|claude)$")
    model: str = Field(min_length=1, max_length=200)
    effort: str = Field(default="", pattern=r"^(|none|low|medium|high|xhigh|max)$")

class ModelRolesRequest(BaseModel):
    supervisor: ModelChoice | None = None
    researcher: ModelChoice | None = None

class AccountRequest(BaseModel):
    provider: str = Field(pattern=r"^(chatgpt|claude)$")
    action: str = Field(pattern=r"^(connect|disconnect|cancel|complete)$")
    code: str | None = Field(None, max_length=8192)

@app.get('/model_settings', dependencies=[Depends(authorize)])
async def model_settings(user_id: uuid.UUID = Header()):
    from .personal_models import get_accounts
    result = await get_accounts().settings(user_id)
    import yaml
    from pathlib import Path
    result['defaults'] = yaml.safe_load(Path(os.getenv('MODEL_TIERS_CONFIG', str(Path(__file__).parents[1] / 'config/model_tiers.yaml'))).read_text()).get('role_models', {})
    return result

@app.get('/model_catalog', dependencies=[Depends(authorize)])
async def model_catalog(user_id: uuid.UUID = Header(), refresh: bool = Query(False)):
    from .personal_models import get_accounts
    return await get_accounts().catalog(user_id, refresh)

@app.post('/model_key', dependencies=[Depends(authorize)])
async def model_key(body: ModelKeyRequest, user_id: uuid.UUID = Header()):
    from .personal_models import get_accounts
    from .model_accounts import AccountError
    try:
        get_accounts().set_key(user_id, body.provider, body.key)
        if not body.key:stop_connection_runs(user_id,body.provider)
    except AccountError as error:
        raise HTTPException(422, str(error)) from None
    return {'saved': True}

@app.post('/model_roles', dependencies=[Depends(authorize)])
async def model_roles(body: ModelRolesRequest, user_id: uuid.UUID = Header()):
    from .personal_models import get_accounts
    from .model_accounts import AccountError
    try:
        await get_accounts().save_roles(user_id, body.model_dump())
    except AccountError as error:
        raise HTTPException(422, str(error)) from None
    return {'saved': True}

@app.post('/model_account', dependencies=[Depends(authorize)])
async def model_account(body: AccountRequest, user_id: uuid.UUID = Header()):
    from .personal_models import get_accounts
    from .model_accounts import AccountError
    service = get_accounts()
    try:
        if body.action == 'connect':
            return await (service.start_chatgpt(user_id) if body.provider == 'chatgpt' else service.start_claude(user_id))
        if body.action == 'complete':
            if body.provider != 'claude':
                raise AccountError('Code submission is only available for Claude Code sign-in')
            return await service.complete_claude(user_id, body.code)
        if body.action == 'cancel':
            await service.cancel_sign_in(user_id, body.provider)
            return {'cancelled': True}
        stop_connection_runs(user_id,body.provider)
        await (service.disconnect_chatgpt(user_id) if body.provider == 'chatgpt' else service.disconnect_claude(user_id))
    except AccountError as error:
        raise HTTPException(422, str(error)) from None
    return {'disconnected': True}

class FlightSelection(BaseModel):
    itinerary_id: uuid.UUID
    uid: str = Field(min_length=1, max_length=255)
    search_index: int = Field(ge=0)
    option_index: int = Field(ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    revision: int | None = Field(None, ge=1)

@app.post('/select_flight', dependencies=[Depends(authorize)])
async def select_flight(body: FlightSelection, user_id: uuid.UUID = Header(), chat_id: uuid.UUID = Query()):
    lock = chat_locks.setdefault((user_id, chat_id), asyncio.Lock())
    async with lock:
        itinerary = db_handler.get_itinerary(user_id, chat_id, body.itinerary_id)
        if not itinerary or 'error' in itinerary:
            raise HTTPException(404, 'Itinerary not found')
        if body.revision and body.revision != itinerary.get('revision', 1):
            raise HTTPException(409, 'This plan changed. Reopen it before choosing a flight.')
        if not any(f['uid'] == body.uid for f in itinerary.get('travel_options', {}).get('flights', [])):
            raise HTTPException(404, 'Flight search not found in this itinerary')
        raw = itinerary.get('flight_selections', {}).get(body.uid) or db_handler.get_tool_response(user_id, chat_id, body.uid)
        from trvelle.tools.flight_selection import choose_flight
        try:
            from trvelle.tools.search_gateway import search_context
            with search_context():
                selected = await asyncio.to_thread(choose_flight, raw, body.search_index, body.option_index)
            updated = db_handler.save_flight_selection(user_id, chat_id, body.itinerary_id, body.uid, selected)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except Exception:
            raise HTTPException(502, 'Flight availability could not be refreshed. Your existing selection is unchanged.')
        from trvelle.utils.currency import preferred_currency, present_currency
        currency = body.currency or preferred_currency(updated.get('summary', {}).get('origin', ''))
        return {'itinerary': await present_currency(updated, currency), 'flight_data': await present_currency(selected, currency)}

class HotelSelection(BaseModel):
    itinerary_id: uuid.UUID
    uid: str = Field(min_length=1, max_length=255)
    stay_key: str = Field(min_length=1, max_length=64)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    revision: int | None = Field(None, ge=1)

class ActivityEdit(BaseModel):
    itinerary_id: uuid.UUID
    day_index: int = Field(ge=0)
    item_index: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=4000)
    location: str = Field(default='', max_length=300)
    start_time: str | None = Field(default=None, pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')
    end_time: str | None = Field(default=None, pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    revision: int | None = Field(None, ge=1)

async def save_owned_edit(body, user_id, chat_id, transform):
    lock = chat_locks.setdefault((user_id, chat_id), asyncio.Lock())
    async with lock:
        itinerary = db_handler.get_itinerary(user_id, chat_id, body.itinerary_id)
        if not itinerary or 'error' in itinerary:
            raise HTTPException(404, 'Itinerary not found')
        if body.revision and body.revision != itinerary.get('revision', 1):
            raise HTTPException(409, 'This plan changed. Reopen it before saving your edit.')
        try:
            raw = transform(itinerary)
            updated = db_handler.save_itinerary_edit(user_id, chat_id, body.itinerary_id, raw, expected_revision=body.revision)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        from trvelle.utils.currency import preferred_currency, present_currency
        currency = body.currency or preferred_currency(updated.get('summary',{}).get('origin',''))
        return {'itinerary':await present_currency(updated,currency)}

@app.post('/select_hotel', dependencies=[Depends(authorize)])
async def select_hotel(body: HotelSelection, user_id: uuid.UUID = Header(), chat_id: uuid.UUID = Query()):
    from trvelle.utils.itinerary_edits import replace_hotel
    return await save_owned_edit(body,user_id,chat_id,lambda itinerary:replace_hotel(itinerary,itinerary.get('travel_options',{}).get('hotels',[]),body.stay_key,body.uid))

@app.post('/edit_activity', dependencies=[Depends(authorize)])
async def update_activity(body: ActivityEdit, user_id: uuid.UUID = Header(), chat_id: uuid.UUID = Query()):
    from trvelle.utils.itinerary_edits import edit_activity
    def transform(itinerary):
        options=itinerary.get('travel_options',{})
        hotel_names=[h.get('name','') for h in options.get('hotels',[])]
        uids=[h['choose_uid'] for h in options.get('hotels',[])]+[f['uid'] for f in options.get('flights',[])]
        return edit_activity(itinerary,body.day_index,body.item_index,body.model_dump(include={'title','description','location','start_time','end_time'}),hotel_names,uids)
    return await save_owned_edit(body,user_id,chat_id,transform)

class ItemAlternativeSelection(BaseModel):
    itinerary_id: uuid.UUID
    item_id: str = Field(min_length=1, max_length=100)
    alternative_id: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=1)
    currency: str | None = Field(None, pattern=r'^[A-Z]{3}$')

@app.post('/select_item_alternative', dependencies=[Depends(authorize)])
async def select_item_alternative(body: ItemAlternativeSelection, user_id: uuid.UUID = Header(), chat_id: uuid.UUID = Query()):
    from trvelle.utils.item_alternatives import replace_item_alternative
    return await save_owned_edit(body, user_id, chat_id, lambda itinerary: replace_item_alternative(itinerary, body.item_id, body.alternative_id))

class RoomPlan(BaseModel):
    itinerary_id: uuid.UUID
    stay_key: str = Field(min_length=1, max_length=64)
    rooms: int = Field(ge=1, le=30)
    revision: int = Field(ge=1)
    currency: str | None = Field(None, pattern=r'^[A-Z]{3}$')

@app.post('/hotel_room_plan', dependencies=[Depends(authorize)])
async def hotel_room_plan(body: RoomPlan, user_id: uuid.UUID = Header(), chat_id: uuid.UUID = Query()):
    def transform(itinerary):
        if not any(h.get('stay_key') == body.stay_key for h in itinerary.get('travel_options', {}).get('hotels', [])):
            raise ValueError('Stay not found in this itinerary.')
        return {**itinerary, 'room_allocations': {**itinerary.get('room_allocations', {}), body.stay_key:body.rooms}}
    return await save_owned_edit(body, user_id, chat_id, transform)

class BudgetAllocation(BaseModel):
    itinerary_id: uuid.UUID
    revision: int = Field(ge=1)
    currency: str = Field(pattern=r'^[A-Z]{3}$')
    activities: float = Field(ge=0, le=1e10, allow_inf_nan=False)
    meals: float = Field(ge=0, le=1e10, allow_inf_nan=False)
    transport: float = Field(ge=0, le=1e10, allow_inf_nan=False)
    buffer: float = Field(ge=0, le=1e10, allow_inf_nan=False)


@app.post('/budget_allocation', dependencies=[Depends(authorize)])
async def update_budget_allocation(body: BudgetAllocation, user_id: uuid.UUID = Header(), chat_id: uuid.UUID = Query()):
    def transform(itinerary):
        return {**itinerary, 'budget_allocations': body.model_dump(include={'currency', 'activities', 'meals', 'transport', 'buffer'})}
    return await save_owned_edit(body, user_id, chat_id, transform)


class DetailRequest(BaseModel):
    itinerary_id: uuid.UUID
    kind: str = Field(pattern=r'^(flight|hotel|activity)$')
    uid: str | None = Field(default=None, max_length=255)
    day_index: int | None = Field(default=None, ge=0)
    item_index: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r'^[A-Z]{3}$')
    revision: int | None = Field(default=None, ge=1)
    purpose: str = Field(default='details', pattern=r'^(details|booking|place)$')
    fetch_photos: bool = False
    fields: list[str] | None = Field(default=None, min_length=1, max_length=6)

@app.post('/fetch_details', dependencies=[Depends(authorize), Depends(model_owner), Depends(detail_budget)])
async def fetch_details(body: DetailRequest, user_id: uuid.UUID = Header(), chat_id: uuid.UUID = Query()):
    from copy import deepcopy
    from trvelle.tools.detail_lookup import DetailLookup, DETAIL_FIELDS
    from trvelle.utils.currency import preferred_currency, present_currency
    if body.purpose == 'booking' and body.kind not in ('hotel','flight'):
        raise HTTPException(422, 'Choose a hotel or flight for booking options')
    if body.purpose == 'place' and body.kind != 'activity':
        raise HTTPException(422, 'Place matching is only available for activities')
    if body.fields and (body.purpose != 'details' or not set(body.fields).issubset(DETAIL_FIELDS[body.kind])):
        raise HTTPException(422, 'Choose details belonging to this travel item')
    lock = chat_locks.setdefault((user_id, chat_id), asyncio.Lock())
    async with lock:
        itinerary = db_handler.get_itinerary(user_id, chat_id, body.itinerary_id)
        if not itinerary or 'error' in itinerary:
            raise HTTPException(404, 'Itinerary not found')
        if body.revision and body.revision != itinerary.get('revision', 1):
            raise HTTPException(409, 'This plan changed. Open its latest version before fetching details.')
        currency = body.currency or preferred_currency(itinerary.get('summary', {}).get('origin',''))
        options = itinerary.get('travel_options', {})
        activity = None
        if body.uid and body.day_index is None:
            for di, day in enumerate(itinerary.get('daily_plan', [])):
                for ii, item in enumerate(day.get('items', [])):
                    if item.get('uid') == body.uid and item.get('card_type') == body.kind:
                        body.day_index, body.item_index = di, ii
                        break
        if body.day_index is not None and body.item_index is not None:
            try:
                activity = itinerary['daily_plan'][body.day_index]['items'][body.item_index]
            except (KeyError, IndexError, TypeError):
                raise HTTPException(404, 'Travel item not found')
            if activity.get('item_type') == 'tag' or activity.get('card_type', 'activity') != body.kind:
                raise HTTPException(422, 'Choose the matching travel item type')
        if body.kind == 'activity' and activity is not None:
            from trvelle.utils.itinerary_edits import edit_activity
            try:
                edit_activity(itinerary, body.day_index, body.item_index, {},
                              [h.get('name', '') for h in options.get('hotels', [])],
                              [h.get('choose_uid') for h in options.get('hotels', [])] + [f.get('uid') for f in options.get('flights', [])])
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
        hotel = next((h for h in options.get('hotels', []) if h.get('choose_uid') == body.uid), None) if body.kind == 'hotel' else None
        flight = next((f for f in options.get('flights', []) if f.get('uid') == body.uid), None) if body.kind == 'flight' else None
        if (body.kind == 'activity' and activity is None) or (body.kind != 'activity' and not (hotel or flight or activity)):
            raise HTTPException(404, 'Travel item not found in this itinerary')
        key = f'{body.kind}:{body.uid}' if hotel or flight else f'{body.kind}:{body.day_index}:{body.item_index}'
        if body.purpose == 'booking':
            key = f'{body.kind}-booking:{body.uid}'
        if body.purpose == 'place':
            key = f'activity-place:{body.day_index}:{body.item_index}'
        if body.fields:
            key += ':' + '|'.join(sorted(set(body.fields)))
        from trvelle.tools.web_search import search_providers
        lookup_kwargs = {'fields': body.fields} if body.fields else {}
        if search_providers()['web'] == 'brave':
            lookup_kwargs['web_provider'] = 'brave'
        # Use the existing quota-aware researcher router, without starting a chat turn.
        try:
            router = get_orchestrator().model_router if body.purpose == 'details' else None
        except HTTPException:
            router = None
        lookup = DetailLookup(router)
        edited = deepcopy(itinerary)
        detail_data = None
        context = ' '.join(str(value) for value in (
            itinerary.get('trip_name', ''), itinerary.get('summary', {}).get('dates', {}).get('start', ''),
            itinerary.get('summary', {}).get('dates', {}).get('end', '')) if value)
        if flight:
            raw = deepcopy(itinerary.get('flight_selections', {}).get(body.uid) or db_handler.get_tool_response(user_id, chat_id, body.uid))
            if not isinstance(raw, list):
                raise HTTPException(422, 'The saved flight search is unavailable')
            if body.purpose=='booking':
                from trvelle.tools.flight_booking import fetch_flight_booking
                try:raw,booking_report=await fetch_flight_booking(raw,currency,lookup)
                except ValueError as error:raise HTTPException(422,str(error)) from None
                reports=[booking_report]
            else:
                reports = []
                for part in raw:
                    choices = part.get('best_flights', []) + part.get('other_flights', [])
                    if not choices:
                        continue
                    index = part.get('selected_option_index', 0)
                    if not isinstance(index, int) or not 0 <= index < len(choices):
                        raise HTTPException(422, 'The selected flight is unavailable')
                    enriched, report = await lookup.fetch('flight', choices[index], part, currency, context, **lookup_kwargs)
                    bucket = 'best_flights' if index < len(part.get('best_flights', [])) else 'other_flights'
                    offset = index if bucket == 'best_flights' else index - len(part.get('best_flights', []))
                    part[bucket][offset] = enriched
                    reports.append(report)
            edited.setdefault('flight_selections', {})[body.uid] = raw
            detail_data = raw
            report = {'summary':'\n\n'.join(r['summary'] for r in reports if r.get('summary')),
                      'sources':list({s['url']:s for r in reports for s in r.get('sources', [])}.values()),
                      'missing':list(dict.fromkeys(x for r in reports for x in r.get('missing', []))),
                      'filled':list(dict.fromkeys(x for r in reports for x in r.get('filled', []))),
                      'provider_notice':' '.join(dict.fromkeys(r['provider_notice'] for r in reports if r.get('provider_notice'))),
                      'fetched_at':reports[-1]['fetched_at'] if reports else None,
                      'status':'partial' if any(r.get('missing') for r in reports) else 'complete',
                      **({'fields':body.fields} if body.fields else {})}
        elif hotel:
            raw = db_handler.get_tool_response(user_id, chat_id, body.uid)
            kwargs = {'booking_only': True} if body.purpose == 'booking' else lookup_kwargs
            hotel_context = ' '.join(str(value) for value in (
                hotel.get('location') or hotel.get('destination') or context,
                hotel.get('check_in_date'), hotel.get('check_out_date'),
                (raw or {}).get('search_parameters', {}).get('adults')) if value)
            enriched, report = await lookup.fetch('hotel', hotel, raw, currency, hotel_context, **kwargs)
            edited.setdefault('hotel_details', {})[body.uid] = enriched
            detail_data = {'properties':[enriched]}
        else:
            from trvelle.tools.web_search import search_providers
            providers = search_providers()
            if body.purpose == 'place':
                from trvelle.tools.place_search import enrich_activity
                from trvelle.tools.search_gateway import SearchBudgetError
                try:
                    enriched, matched = await enrich_activity(activity, destination=itinerary['daily_plan'][body.day_index].get('destination') or '', photos=body.fetch_photos)
                    report = {'summary':'', 'sources':[], 'status':'complete' if matched or enriched.get('place_details') else 'unavailable',
                        'filled':['Place details'] if matched else [], 'missing':[], 'fetched_at':datetime.now(timezone.utc).isoformat()}
                except SearchBudgetError as error:
                    enriched = activity
                    report = {'summary':'', 'sources':[], 'status':'unavailable', 'filled':[], 'missing':[], 'provider_notice':str(error)}
            else:
                kwargs = lookup_kwargs
                enriched, report = await lookup.fetch(body.kind, activity, None, currency, activity.get('location') or context, **kwargs)
            edited['daily_plan'][body.day_index]['items'][body.item_index] = enriched
        edited.setdefault('detail_reports', {})[key] = report
        updated = db_handler.save_itinerary_edit(user_id, chat_id, body.itinerary_id, edited)
        return {'itinerary':await present_currency(updated, currency),
                'detail_data':await present_currency(detail_data, currency) if detail_data else None,
                'report':report}

@app.get('/flight_booking',dependencies=[Depends(authorize)])
async def flight_booking(user_id:uuid.UUID=Header(),chat_id:uuid.UUID=Query(),itinerary_id:uuid.UUID=Query(),uid:str=Query(),offer_index:int=Query(ge=0),revision:int=Query(ge=1)):
    from trvelle.tools.flight_booking import booking_offers
    itinerary=db_handler.get_itinerary(user_id,chat_id,itinerary_id)
    if not itinerary or 'error' in itinerary:raise HTTPException(404,'Itinerary not found')
    if itinerary.get('revision',1)!=revision:raise HTTPException(409,'This itinerary changed. Reopen flight details before booking.')
    raw=itinerary.get('flight_selections',{}).get(uid)
    if raw is None:raw=db_handler.get_tool_response(user_id,chat_id,uid)
    if not isinstance(raw,list):raise HTTPException(404,'Flight not found')
    if not any(flight.get('uid')==uid for flight in itinerary.get('travel_options',{}).get('flights',[])):raise HTTPException(404,'Flight not found in this itinerary')
    offers=booking_offers(raw)
    offer=next((entry for entry in offers if entry['source_index']==offer_index),None)
    if offer is None:raise HTTPException(404,'Reopen flight details to load the booking option')
    from trvelle.tools.affiliate_links import affiliate_links
    booking = dict(offer['booking_request'])
    booking['url'] = await affiliate_links.resolve(booking['url'], 'flight', post_data=booking.get('post_data',''))
    return {'provider':offer.get('book_with','Booking provider'),'booking_request':booking}


@app.get('/hotel_booking', dependencies=[Depends(authorize)])
async def hotel_booking(user_id:uuid.UUID=Header(), chat_id:uuid.UUID=Query(), itinerary_id:uuid.UUID=Query(), uid:str=Query(), offer_index:int=Query(ge=-1), revision:int=Query(ge=1)):
    from trvelle.tools.affiliate_links import affiliate_links, hotel_booking_offers
    from trvelle.tools.flight_booking import safe_booking_url
    itinerary = db_handler.get_itinerary(user_id, chat_id, itinerary_id)
    if not itinerary or 'error' in itinerary: raise HTTPException(404, 'Itinerary not found')
    if itinerary.get('revision',1) != revision: raise HTTPException(409, 'This itinerary changed. Reopen hotel details before booking.')
    hotel = next((h for h in itinerary.get('travel_options',{}).get('hotels',[]) if h.get('choose_uid') == uid), None)
    if not hotel: raise HTTPException(404, 'Hotel not found in this itinerary')
    if offer_index == -1:
        url = safe_booking_url(hotel.get('link'))
    else:
        offers = hotel_booking_offers(hotel)
        offer = next((entry for entry in offers if entry['source_index'] == offer_index), None)
        url = offer['url'] if offer else None
    if not url: raise HTTPException(404, 'Reopen hotel details to load this booking option')
    return {'url':await affiliate_links.resolve(url, 'hotel')}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8001")))
