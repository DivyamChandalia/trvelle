"""Owned durable-run API and backwards-compatible chat streaming wrappers."""
import asyncio
import json
import uuid
import os
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from .api_wrapper import authorize, ChatRequest, db_handler
from .run_store import store, RunConflict, TERMINAL
from .personal_models import get_accounts
from trvelle.utils.currency import preferred_currency
from trvelle.tools.web_search import search_providers

router = APIRouter(dependencies=[Depends(authorize)])


def snapshot(owner, run_id=None, chat_id=None):
    try:
        return store.snapshot(owner, run_id, chat_id)
    except LookupError:
        raise HTTPException(404, 'Planning run not found') from None


def stream(owner, run_id, after=0, history=False):
    run = snapshot(owner, run_id)
    async def events():
        yield f"event: chat_id\ndata: {json.dumps(run['chat_id'])}\n\n"
        yield f"event: message_id\ndata: {json.dumps(run['run_id'])}\n\n"
        if history:
            messages = db_handler.get_filtered_chat_history(owner, uuid.UUID(run['chat_id']))
            yield f"event: history\ndata: {json.dumps(messages)}\n\n"
        cursor = after
        # Capture snapshot/cursor together before replay so saved text isn't repeated.
        if history and not after:
            cursor = run['last_event_id']
            yield f"event: run\ndata: {json.dumps(run)}\n\n"
        last_keepalive = 0
        while True:
            rows = store.events(owner, run_id, cursor)
            for event in rows:
                cursor = event['id']
                yield f"id: {cursor}\nevent: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
                if event['event'] == 'done':
                    return
            current = snapshot(owner, run_id)
            if current['status'] in TERMINAL and cursor >= current['last_event_id']:
                yield 'event: done\ndata: ""\n\n'
                return
            last_keepalive += 1
            if last_keepalive >= 30:
                last_keepalive = 0
                yield ': keepalive\n\n'
            await asyncio.sleep(.5)
    return StreamingResponse(events(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@router.post('/runs/start')
async def start(body: ChatRequest, user_id: uuid.UUID = Header(), chat_id: uuid.UUID | None = Query(None)):
    identifier = chat_id or uuid.uuid4()
    currency = body.currency or preferred_currency(body.message)
    if chat_id and not body.currency:
        from trvelle.database.models import ChatSession, ToolExecution
        with db_handler.db_session() as db:
            owned = db.query(ChatSession).filter_by(chat_id=identifier, user_id=user_id).first()
            if not owned:
                raise HTTPException(404, 'Chat not found')
            previous = db.query(ToolExecution).filter_by(chat_id=identifier, tool_name='itinerary_tool').order_by(ToolExecution.created_at.desc()).first()
            if previous:
                currency = preferred_currency(previous.raw_response.get('summary', {}).get('origin', ''), currency)
    # Keys and OAuth tokens stay in the encrypted account store; snapshot only choices.
    from trvelle.tools.search_gateway import brave_run_budget
    brave_maximum, photo_reserve = brave_run_budget()
    providers = search_providers()
    request = {'message': body.message, 'currency': currency, 'choices': get_accounts().load(user_id)['roles'],
        'replace_message_id': str(body.replace_message_id) if body.replace_message_id else None,
        'search_limits': {'serpapi':6, 'tavily':6, 'brave':brave_maximum},
        'search_reserves': {'brave_places':photo_reserve if providers['places']=='brave' else 0},
        'search_providers': providers}
    if chat_id and not body.replace_message_id:
        from trvelle.utils.itinerary_patch import update_scope
        scope = update_scope(body.message)
        if scope:
            from trvelle.database.models import ToolExecution, Message, PlanningRun
            with db_handler.db_session() as db:
                previous = db.query(ToolExecution).join(Message, Message.message_id == ToolExecution.message_id).filter(
                    ToolExecution.chat_id == identifier, ToolExecution.tool_name == 'itinerary_tool', Message.superseded.is_(False)).order_by(ToolExecution.created_at.desc()).first()
                if previous and not (previous.raw_response or {}).get('daily_plan'):
                    previous=None
                if previous:
                    itinerary_id=previous.message_id
                    prior_run=db.query(PlanningRun).filter_by(user_id=user_id,chat_id=identifier).order_by(PlanningRun.created_at.desc()).all()
                    cached_base=next(((run.request or {}).get('context_base') for run in prior_run if (run.request or {}).get('base_itinerary_id')==str(itinerary_id) and (run.request or {}).get('context_base')),None)
                    base_revision=(previous.raw_response or {}).get('revision',1)
                if previous:
                    from trvelle.utils.edit_context import context_snapshot,edit_diff
                    current_view=db_handler.get_itinerary(user_id,identifier,itinerary_id)
                    base_revision=current_view.get('revision',1)
                    if cached_base is None:
                        cached_base=context_snapshot(db_handler.get_itinerary(user_id,identifier,itinerary_id,revision=1))
                    request.update(mode='update',update_scope=scope,base_itinerary_id=str(itinerary_id),base_revision=base_revision,
                                   context_base=cached_base,context_diff=edit_diff(cached_base,context_snapshot(current_view)),
                                   search_limits={'serpapi':6 if scope=='inventory' else 0,'tavily':2,'brave':min(brave_maximum,8)},search_reserves={'brave_places':2 if providers['places']=='brave' else 0})
    try:
        return store.create(user_id, identifier, request)
    except LookupError:
        raise HTTPException(404, 'Chat not found') from None
    except RunConflict as error:
        raise HTTPException(409, str(error)) from None


@router.get('/runs/status')
async def status(user_id: uuid.UUID = Header(), run_id: uuid.UUID | None = Query(None), chat_id: uuid.UUID | None = Query(None)):
    if not (run_id or chat_id):
        raise HTTPException(422, 'Supply run_id or chat_id')
    return snapshot(user_id, run_id, chat_id)


@router.get('/runs/events')
async def events(user_id: uuid.UUID = Header(), run_id: uuid.UUID = Query(), after: int = Query(0, ge=0), last_event_id: str | None = Header(None)):
    if last_event_id and last_event_id.isdigit():
        after = max(after, int(last_event_id))
    return stream(user_id, run_id, after, history=not after)


class RunAction(BaseModel):
    run_id: uuid.UUID
    message: str | None = Field(None, min_length=1, max_length=20000)
    serpapi: int = Field(0, ge=0, le=6)
    tavily: int = Field(0, ge=0, le=6)
    brave: int = Field(0, ge=0, le=6)


def endpoint(action):
    async def control(body: RunAction, user_id: uuid.UUID = Header()):
        if action == 'steer' and not body.message:
            raise HTTPException(422, 'A change needs a message')
        try:
            return store.control(user_id, body.run_id, action, body.message)
        except LookupError:
            raise HTTPException(404, 'Planning run not found') from None
        except RunConflict as error:
            raise HTTPException(409, str(error)) from None
    return control


for action in ('resume', 'stop', 'finish', 'steer', 'interrupt'):
    router.add_api_route('/runs/' + action, endpoint(action), methods=['POST'], name=action + '_run')


@router.post('/runs/extend_budget')
async def extend(body: RunAction, user_id: uuid.UUID = Header()):
    snapshot(user_id, body.run_id)
    from trvelle.database.models import PlanningRun
    with store.sessions() as db:
        run = db.query(PlanningRun).filter_by(run_id=body.run_id, user_id=user_id).with_for_update().one()
        limits = run.request['search_limits']
        run.request = {**run.request, 'search_limits': {**limits, 'serpapi':min(12,limits['serpapi']+body.serpapi), 'tavily':min(12,limits['tavily']+body.tavily), 'brave':min(24,limits.get('brave',0)+body.brave)}}
        db.commit()
    return snapshot(user_id, body.run_id)


@router.post('/chat')
async def chat(body: ChatRequest, user_id: uuid.UUID = Header(), chat_id: uuid.UUID | None = Query(None)):
    if body.resume and chat_id:
        return await resume_chat(user_id, chat_id)
    run = await start(body, user_id, chat_id)
    return stream(user_id, uuid.UUID(run['run_id'])) if body.stream else run


@router.post('/chat_resume')
async def resume_chat(user_id: uuid.UUID = Header(), chat_id: uuid.UUID = Query()):
    try:
        run = store.snapshot(user_id, chat_id=chat_id)
    except LookupError:
        # Import old saved research without removing messages or pretending it is active.
        from trvelle.database.models import Message, ChatSession
        with db_handler.db_session() as db:
            if not db.query(ChatSession).filter_by(user_id=user_id, chat_id=chat_id).first():
                raise HTTPException(404, 'Chat not found')
            previous = db.query(Message).filter_by(chat_id=chat_id, user_id=user_id, type='human', superseded=False).order_by(Message.created_at.desc()).first()
            if not previous:
                raise HTTPException(409, 'There is no saved request to resume')
            query = previous.content
            answer = db.query(Message).filter_by(chat_id=chat_id, user_id=user_id, type='ai', message_name='Supervisor_Agent', superseded=False).order_by(Message.created_at.desc()).first()
            if answer and answer.created_at > previous.created_at and answer.content and not (answer.additional_kwargs or {}).get('tool_calls'):
                async def completed():
                    yield f'event: chat_id\ndata: {json.dumps(str(chat_id))}\n\n'
                    yield f'event: message_id\ndata: {json.dumps(str(answer.message_id))}\n\n'
                    yield f'event: history\ndata: {json.dumps(db_handler.get_filtered_chat_history(user_id, chat_id))}\n\n'
                    yield 'event: done\ndata: ""\n\n'
                return StreamingResponse(completed(), media_type='text/event-stream')
        run = await start(ChatRequest(message=query), user_id, chat_id)
        store.started(uuid.UUID(run['run_id']))
    try:
        run = store.control(user_id, uuid.UUID(run['run_id']), 'resume')
    except RunConflict as error:
        raise HTTPException(409, str(error)) from None
    return stream(user_id, uuid.UUID(run['run_id']), history=True)


@router.get('/diagnostics')
async def diagnostics(user_id: uuid.UUID = Header()):
    from trvelle.tools.search_gateway import gateway
    result = gateway.status()
    result['active_runs'] = []
    from trvelle.database.models import PlanningRun
    with store.sessions() as db:
        ids = [run.run_id for run in db.query(PlanningRun).filter_by(user_id=user_id).filter(PlanningRun.status.in_(['queued', 'running'])).all()]
    result['active_runs'] = [snapshot(user_id, identifier) for identifier in ids]
    return result
