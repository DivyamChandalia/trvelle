"""Independent local planning worker: python -m trvelle.orchestrator.worker."""
import asyncio
from datetime import date, datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import signal
import time
import uuid
from langchain_core.messages import ToolMessage
from trvelle.utils import load_environment
load_environment()
from .run_store import store, RunConflict
from .client import Orchestrator
from .personal_models import active_owner, active_choices, get_accounts
from trvelle.tools.search_gateway import search_context
from trvelle.tools.place_search import enrich_itinerary
from trvelle.database.models import ToolExecution, PlanningRun, Message

logger = logging.getLogger(__name__)


async def publish_researched_draft(agent, config, reason):
    """Retain a rejected structured schedule, with honest gaps and valid references."""
    from copy import deepcopy
    from trvelle.tools.itinerary_tool import Itinerary, itinerary_tool
    from trvelle.utils.secrets import redact_secrets
    with agent.db_handler.db_session() as db:
        run = db.get(PlanningRun, config['run_id'])
        since = run.created_at.replace(tzinfo=None)
        if db.query(ToolExecution).filter(ToolExecution.chat_id == config['chat_id'],
            ToolExecution.tool_name == 'itinerary_tool', ToolExecution.created_at >= since).first():
            return False
        messages = db.query(Message).filter(Message.chat_id == config['chat_id'], Message.type == 'ai',
            Message.created_at >= since).order_by(Message.created_at.desc()).all()
        candidates = [deepcopy(call.get('args', {}).get('itinerary')) for row in messages
            for call in (row.additional_kwargs or {}).get('tool_calls', []) if call.get('name') == 'itinerary_tool']
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate['planning_status'] = 'partial'
        candidate['unfinished'] = list(dict.fromkeys([*(candidate.get('unfinished') or []), 'Travel quotes', 'Full trip budget']))
        try:
            raw = Itinerary.model_validate(candidate).model_dump(mode='json')
        except (ValueError, TypeError):
            continue
        if agent.db_handler.itinerary_search_issue(config['user_id'], config['chat_id'], raw):
            continue  # A draft still cannot contain invented flight or hotel IDs.
        with search_context(config['run_id'], config.get('search_limits'), config.get('search_providers')):
            raw = await enrich_itinerary(raw)
        result = await itinerary_tool(Itinerary.model_validate(raw))
        identifier = uuid.uuid4()
        message = ToolMessage(content=result['result'], tool_call_id=str(identifier), name='itinerary_tool')
        agent.db_handler.save_message_to_db(message, config)
        with agent.db_handler.db_session() as db:
            db.add(ToolExecution(message_id=identifier, chat_id=config['chat_id'], tool_name='itinerary_tool', raw_response=redact_secrets(result['raw'])))
            db.commit()
        store.checkpoint(config['run_id'], 'publish', itinerary_id=str(identifier))
        from trvelle.utils.travel_text import itinerary_chat_text
        store.event(config['run_id'], 'message_id', str(identifier))
        store.event(config['run_id'], 'message', itinerary_chat_text(result['raw']))
        store.event(config['run_id'], 'itinerary_id', str(identifier))
        return True
    return False


async def publish_partial(agent, config, reason):
    """Publish only saved evidence when stopped by time/call limits; never fabricate a day."""
    if await publish_researched_draft(agent, config, reason):
        return True
    with agent.db_handler.db_session() as db:
        run = db.get(PlanningRun, config['run_id'])
        rows = db.query(ToolExecution).filter(ToolExecution.chat_id == config['chat_id'], ToolExecution.created_at >= run.created_at.replace(tzinfo=None)).order_by(ToolExecution.created_at).all()
        previous = next((row for row in reversed(rows) if row.tool_name == 'itinerary_tool'), None)
        if previous:
            from trvelle.utils.travel_text import itinerary_chat_text
            store.checkpoint(config['run_id'], 'publish', itinerary_id=str(previous.message_id))
            store.event(config['run_id'], 'message_id', str(previous.message_id))
            store.event(config['run_id'], 'message', itinerary_chat_text(previous.raw_response or {}))
            store.event(config['run_id'], 'itinerary_id', str(previous.message_id))
            return True
        flight = next((row for row in rows if row.tool_name == 'flight_search' and isinstance(row.raw_response, list)), None)
        hotel = next((row for row in rows if row.tool_name == 'hotel_search' and isinstance(row.raw_response, dict) and row.raw_response.get('properties')), None)
        if not flight and not hotel:
            return False
        params = hotel.raw_response.get('search_parameters', {}) if hotel else next((part.get('search_parameters', {}) for part in flight.raw_response if isinstance(part, dict) and part.get('search_parameters')), {})
        start = params.get('check_in_date') or params.get('outbound_date')
        end = params.get('check_out_date') or params.get('return_date') or start
        try:
            first, last = date.fromisoformat(start), date.fromisoformat(end)
        except (ValueError, TypeError):
            return False
        days = [{'day': index + 1, 'date': (first + timedelta(days=index)).isoformat(), 'items': []} for index in range(min(31, (last - first).days + 1))]
        if not days:
            return False
        if flight:
            uid = next((part.get('choose_uid') for part in flight.raw_response if part.get('choose_uid')), None)
            if uid:
                days[0]['items'].append({'item_type': 'card', 'card_type': 'flight', 'uid': uid, 'title': 'Saved flight option'})
        if hotel:
            candidates = [h for h in hotel.raw_response['properties'] if h.get('choose_uid')]
            if candidates:
                chosen = candidates[0]
                days[0]['items'].append({'item_type': 'card', 'card_type': 'hotel', 'uid': chosen['choose_uid'], 'title': chosen['name']})
        for day in days:
            day['items'].append({'item_type': 'tag', 'card_type': 'note', 'title': 'Day still to plan', 'description': 'Activities and transfers have not been verified yet.'})
        raw = {'trip_name': params.get('q') or params.get('arrival_id') or 'Saved trip draft',
            'trip_description': reason, 'summary': {'dates': {'start': start, 'end': end},
            'travelers': params.get('adults', 1), 'currency': config['currency']}, 'daily_plan': days,
            'planning_status': 'partial', 'unfinished': ['Daily activities', 'Transfers', 'Room suitability', 'Full trip budget']}
    from trvelle.tools.itinerary_tool import Itinerary, itinerary_tool
    result = await itinerary_tool(Itinerary.model_validate(raw))
    result['raw'].update(planning_status='partial', unfinished=raw['unfinished'])
    identifier = uuid.uuid4()
    message = ToolMessage(content=result['result'], tool_call_id=str(identifier), name='itinerary_tool')
    agent.db_handler.save_message_to_db(message, config)
    with agent.db_handler.db_session() as db:
        db.add(ToolExecution(message_id=identifier, chat_id=config['chat_id'], tool_name='itinerary_tool', raw_response=result['raw']))
        db.commit()
    store.checkpoint(config['run_id'], 'publish', itinerary_id=str(identifier))
    from trvelle.utils.travel_text import itinerary_chat_text
    store.event(config['run_id'], 'message_id', str(identifier))
    store.event(config['run_id'], 'message', itinerary_chat_text(result['raw']))
    store.event(config['run_id'], 'itinerary_id', str(identifier))
    return True


async def perform(agent, config):
    run_id = config['run_id']
    owner_token = active_owner.set(str(config['user_id']))
    account = get_accounts().load(config['user_id'])
    if config.get('choices') is not None:
        account = {**account, 'roles': config['choices']}
    choice_token = active_choices.set(account)
    completion_status = 'complete'
    published = False
    try:
        if config.get('resume') and config.get('existing_itinerary_id'):
            plan = agent.db_handler.get_itinerary(config['user_id'], config['chat_id'], uuid.UUID(config['existing_itinerary_id']))
            if plan.get('planning_status') == 'partial':
                completion_status = 'partial'
                config['continuation_plan'] = plan
        with search_context(run_id, config['search_limits'], config.get('search_providers')):
            agent.load_chat_history(config)
            if not config['resume']:
                # Store the initial message before marking the checkpoint, so a restart
                # can detect it even if it happened between the two commits.
                with agent.db_handler.db_session() as db:
                    run = db.get(PlanningRun, run_id)
                    exists = db.query(Message).filter(Message.chat_id == config['chat_id'], Message.type == 'human', Message.created_at >= run.created_at.replace(tzinfo=None)).first()
                if not exists:
                    if config.get('replace_message_id'):
                        agent.db_handler.rewind_chat_turn(config['user_id'], config['chat_id'], uuid.UUID(config['replace_message_id']))
                        agent.chat_manager.remove_chat(agent._get_chat_history_key(config))
                        agent.load_chat_history(config)
                    agent.get_message(config['query'], config)
                else:
                    config['turn_message_id'] = str(exists.message_id)
                store.started(run_id)
            async for chunk in agent.orchestrate_stream(config['query'], config, resume=True):
                for key, kind in (('message_id', 'message_id'), ('message', 'message'), ('progress', 'progress'), ('itinerary', 'itinerary_id')):
                    if key in chunk:
                        if key == 'itinerary':
                            store.checkpoint(run_id, 'publish', itinerary_id=chunk[key])
                            published = True
                            plan = agent.db_handler.get_itinerary(config['user_id'], config['chat_id'], uuid.UUID(chunk[key]))
                            completion_status = 'partial' if plan.get('planning_status') == 'partial' else 'complete'
                        store.event(run_id, kind, chunk[key])
            if config.get('publication_attempted') and not published:
                raise RunConflict('The full plan could not be published. Saving the researched itinerary as a draft.')
            store.finish(run_id, completion_status)
    finally:
        active_choices.reset(choice_token)
        active_owner.reset(owner_token)


async def execute(agent, config, worker_id):
    task = asyncio.create_task(perform(agent, config))
    last = time.monotonic()
    last_event = last
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=1)
            if task.done():
                break
            now = time.monotonic()
            control = store.heartbeat(config['run_id'], worker_id, now - last)
            last = now
            heartbeat = Path(os.getenv('TRVELLE_WORKER_HEARTBEAT', '.runtime/worker-heartbeat.json'))
            heartbeat.write_text(json.dumps({'worker_id': worker_id, 'run_id': str(config['run_id']), 'updated_at': datetime.now(timezone.utc).isoformat()}))
            if now - last_event >= 5:
                store.event(config['run_id'], 'run', store.snapshot(config['user_id'], config['run_id']))
                last_event = now
            if control:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if control == 'interrupt':
                    # Close only unfinished model tool calls; keep all completed search evidence.
                    agent.recover_failed_turn(config)
                    store.checkpoint(config['run_id'], 'clarify', task='Applying your change')
                    store.requeue(config['run_id'])
                    return
                if control in ('finish', 'deadline'):
                    published = await publish_partial(agent, config, 'Draft made from saved search results. Planning stopped before all details were verified.')
                    store.finish(config['run_id'], 'partial' if published else 'paused', None if published else 'No verified travel options are saved yet. Resume planning to continue.')
                else:
                    store.finish(config['run_id'], 'stopped')
                return
        await task
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # Graceful shutdown releases the lease; a new worker can pick up the checkpoint.
        with store.sessions() as db:
            run = db.get(PlanningRun, config['run_id'])
            if run.status == 'running':
                run.status, run.worker_id, run.lease_until = 'queued', None, None
                db.commit()
        raise
    except RunConflict as error:
        published = await publish_partial(agent, config, str(error))
        store.finish(config['run_id'], 'partial' if published else 'paused', None if published else str(error))
    except Exception as error:
        from .model_router import ModelsUnavailableError
        # Avoid raw provider errors/URLs/tokens in public events and logs.
        message = str(error) if isinstance(error, ModelsUnavailableError) else 'Planning paused because a step could not finish. Your completed research is saved; reconnect or resume to continue.'
        logger.error('Planning run %s paused: %s', config['run_id'], type(error).__name__)
        store.finish(config['run_id'], 'failed', message)


async def main():
    worker_id = f'local-{os.getpid()}-{uuid.uuid4().hex[:8]}'
    agent = Orchestrator()
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)
    heartbeat = Path(os.getenv('TRVELLE_WORKER_HEARTBEAT', '.runtime/worker-heartbeat.json'))
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    try:
        while not stopping.is_set():
            heartbeat.write_text(json.dumps({'worker_id': worker_id, 'updated_at': datetime.now(timezone.utc).isoformat()}))
            config = store.claim(worker_id)
            if config:
                running = asyncio.create_task(execute(agent, config, worker_id))
                stop = asyncio.create_task(stopping.wait())
                done, _ = await asyncio.wait({running, stop}, return_when=asyncio.FIRST_COMPLETED)
                if stop in done:
                    running.cancel()
                stop.cancel()
                await asyncio.gather(running, stop, return_exceptions=True)
            else:
                try:
                    await asyncio.wait_for(stopping.wait(), 1)
                except TimeoutError:
                    pass
    finally:
        heartbeat.unlink(missing_ok=True)
        await agent.shutdown()
        await get_accounts().close()


if __name__ == '__main__':
    asyncio.run(main())
