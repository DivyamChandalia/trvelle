"""Transactional planning state. No credentials are stored in requests or events."""
from datetime import datetime, timedelta, timezone
import json
import uuid
from sqlalchemy import func
from trvelle.database import DBHandler
from trvelle.database.models import User, ChatSession, Message, PlanningRun, RunEvent, RunOperation, SearchCharge
from trvelle.database.chat_versions import selected_run_id

ACTIVE = {'queued', 'running'}
TERMINAL = {'complete', 'partial', 'failed', 'paused', 'stopped'}


def utcnow():
    return datetime.now(timezone.utc)


class RunConflict(ValueError):
    pass


class RunStore:
    def __init__(self, sessions=None):
        self.sessions = sessions or DBHandler.db_session

    def create(self, owner, chat_id, request):
        with self.sessions() as db:
            if not db.get(User, owner):
                db.add(User(user_id=owner))
                db.flush()
            chat = db.get(ChatSession, chat_id)
            if chat and chat.user_id != owner:
                raise LookupError('Chat not found')
            if chat is None:
                chat = ChatSession(chat_id=chat_id, user_id=owner)
                db.add(chat)
                db.flush()
            db.query(ChatSession).filter_by(chat_id=chat_id).with_for_update().one()
            if db.query(PlanningRun).filter(PlanningRun.chat_id == chat_id, PlanningRun.status.in_(ACTIVE)).first():
                raise RunConflict('Planning is already running. Send a change or reconnect to it.')
            if request.get('replace_message_id'):
                target = db.query(Message).filter_by(message_id=request['replace_message_id'], chat_id=chat_id, user_id=owner, type='human', superseded=False).first()
                if not target:
                    raise LookupError('Message not found in the selected conversation')
            run = PlanningRun(user_id=owner, chat_id=chat_id, request=request, checkpoint={'models': request.get('choices', {})}, counters={}, steering=[])
            db.add(run)
            db.flush()
            identifier = run.run_id
            db.commit()
        self.event(identifier, 'run', self.snapshot(owner, identifier))
        return self.snapshot(owner, identifier)

    def snapshot(self, owner, run_id=None, chat_id=None):
        with self.sessions() as db:
            query = db.query(PlanningRun).filter_by(user_id=owner)
            if run_id:
                run = query.filter_by(run_id=run_id).first()
            else:
                query = query.filter_by(chat_id=chat_id)
                run = query.filter(PlanningRun.status.in_(ACTIVE)).order_by(PlanningRun.created_at.desc()).first()
                if run is None:
                    selected = selected_run_id(db, owner, chat_id)
                    run = query.filter_by(run_id=selected).first() if selected else None
                    db.commit()
            if not run:
                raise LookupError('Planning run not found')
            usage = {provider: int(db.query(func.coalesce(func.sum(SearchCharge.credits), 0)).filter_by(run_id=run.run_id, provider=provider).scalar()) for provider in ('serpapi', 'tavily', 'brave')}
            return {'run_id': str(run.run_id), 'chat_id': str(run.chat_id), 'status': run.status,
                'phase': run.phase, 'mode':run.request.get('mode','plan'), 'revision': run.revision, 'last_event_id': run.last_event_id,
                'elapsed_seconds': round(run.active_seconds), 'counters': run.counters,
                'search_usage': usage, 'search_limits': run.request.get('search_limits', {}),
                'search_providers':run.request.get('search_providers',{}),
                'models': run.checkpoint.get('models', {}), 'task': run.checkpoint.get('task', ''),
                'findings': run.checkpoint.get('findings', []), 'sources': run.checkpoint.get('sources', []),
                'queued_changes': run.steering, 'error': run.error,
                'itinerary_id': run.checkpoint.get('itinerary_id'),
                'can_resume': run.status in ('failed', 'paused', 'stopped', 'partial') and run.active_seconds < 600 and run.counters.get('supervisor', 0) < 8,
                'updated_at': run.updated_at.isoformat()}

    def event(self, run_id, kind, data):
        with self.sessions() as db:
            run = db.query(PlanningRun).filter_by(run_id=run_id).with_for_update().one()
            run.last_event_id += 1
            run.updated_at = utcnow()
            db.add(RunEvent(run_id=run_id, event_id=run.last_event_id, kind=kind, data=data))
            db.commit()

    def events(self, owner, run_id, after=0):
        self.snapshot(owner, run_id)
        with self.sessions() as db:
            return [{'id': row.event_id, 'event': row.kind, 'data': row.data} for row in db.query(RunEvent).filter(RunEvent.run_id == run_id, RunEvent.event_id > after).order_by(RunEvent.event_id).limit(200).all()]

    def claim(self, worker_id):
        with self.sessions() as db:
            # A stale lease means the process died. Retain checkpoints, counters and results.
            stale = db.query(PlanningRun).filter(PlanningRun.status == 'running', PlanningRun.lease_until < utcnow()).with_for_update(skip_locked=True).all()
            for run in stale:
                run.status, run.worker_id, run.lease_until = 'queued', None, None
            db.flush()
            run = db.query(PlanningRun).filter_by(status='queued').order_by(PlanningRun.created_at).with_for_update(skip_locked=True).first()
            if not run:
                db.commit()
                return None
            run.status, run.worker_id, run.lease_until = 'running', worker_id, utcnow() + timedelta(seconds=20)
            config = {'user_id': run.user_id, 'chat_id': run.chat_id, 'run_id': run.run_id, 'currency': run.request['currency'],
                'query': run.request['message'], 'choices': run.request.get('choices'), 'resume': bool(run.checkpoint.get('started')),
                'replace_message_id': run.request.get('replace_message_id'), 'search_limits': run.request['search_limits'], 'search_providers':run.request.get('search_providers',{}),
                'existing_itinerary_id': run.checkpoint.get('itinerary_id'),
                **{key:run.request.get(key) for key in ('mode','update_scope','base_itinerary_id','base_revision','context_base','context_diff')}}
            db.commit()
            return config

    def heartbeat(self, run_id, worker_id, seconds):
        with self.sessions() as db:
            run = db.query(PlanningRun).filter_by(run_id=run_id, worker_id=worker_id, status='running').with_for_update().first()
            if not run:
                return 'stop'
            run.active_seconds += max(0, seconds)
            run.lease_until, run.updated_at = utcnow() + timedelta(seconds=20), utcnow()
            control = run.control
            if run.active_seconds >= 600:
                control = 'deadline'
            db.commit()
            return control

    def checkpoint(self, run_id, phase=None, **values):
        with self.sessions() as db:
            run = db.query(PlanningRun).filter_by(run_id=run_id).with_for_update().one()
            run.checkpoint = {**run.checkpoint, **values}
            if phase:
                run.phase = phase
            run.updated_at = utcnow()
            db.commit()

    def reserve_model(self, run_id, role, segment=None):
        key = role if segment is None else f'{role}:{segment}'
        maximum = 8 if role == 'supervisor' else 4
        with self.sessions() as db:
            run = db.query(PlanningRun).filter_by(run_id=run_id).with_for_update().one()
            counters = dict(run.counters)
            count = counters.get(key, 0)
            if count >= maximum:
                raise RunConflict(f'{role} model budget reached. Publish the saved results.')
            counters[key] = count + 1
            run.counters = counters
            db.commit()
            return f'model:{key}:{count + 1}'

    def operation(self, run_id, operation_id, kind, result=None):
        with self.sessions() as db:
            row = db.get(RunOperation, (run_id, operation_id))
            if row and row.status == 'complete' and result is None:
                return row.result
            if row is None:
                row = RunOperation(run_id=run_id, operation_id=operation_id, kind=kind)
                db.add(row)
            if result is not None:
                row.result, row.status = result, 'complete'
            db.commit()
            return None

    def started(self, run_id):
        self.checkpoint(run_id, started=True)

    def requeue(self, run_id):
        """Hand off an interrupted step without terminating connected event streams."""
        with self.sessions() as db:
            run = db.query(PlanningRun).filter_by(run_id=run_id).with_for_update().one()
            run.status, run.control, run.error = 'queued', None, None
            run.worker_id, run.lease_until, run.updated_at = None, None, utcnow()
            owner = run.user_id
            db.commit()
        self.event(run_id, 'run', self.snapshot(owner, run_id))

    def finish(self, run_id, status, error=None):
        with self.sessions() as db:
            run = db.query(PlanningRun).filter_by(run_id=run_id).with_for_update().one()
            run.status, run.error, run.control = status, error, None
            run.worker_id, run.lease_until, run.updated_at = None, None, utcnow()
            owner = run.user_id
            db.commit()
        self.event(run_id, 'run', self.snapshot(owner, run_id))
        if error:
            self.event(run_id, 'error', error)
        self.event(run_id, 'done', '')

    def control(self, owner, run_id, action, message=None):
        with self.sessions() as db:
            if action == 'resume':
                candidate = db.query(PlanningRun).filter_by(run_id=run_id, user_id=owner).first()
                if candidate:
                    selected = selected_run_id(db, owner, candidate.chat_id)
                    if selected and selected != str(run_id):
                        raise RunConflict('Choose that message version before continuing its planning')
            run = db.query(PlanningRun).filter_by(run_id=run_id, user_id=owner).with_for_update().first()
            if not run:
                raise LookupError('Planning run not found')
            if action == 'resume':
                if run.status in ('complete', 'queued', 'running'):
                    pass
                elif run.active_seconds >= 600 or run.counters.get('supervisor', 0) >= 8:
                    raise RunConflict('This run reached its planning budget. Keep the draft or start a new request.')
                else:
                    run.status, run.control, run.error = 'queued', None, None
            elif action in ('stop', 'finish', 'interrupt'):
                if run.status not in ACTIVE:
                    raise RunConflict('Planning is no longer active.')
                run.control = action
                if action == 'stop' and run.status == 'queued':
                    run.status, run.control = 'stopped', None
            elif action == 'steer':
                if run.status not in ACTIVE:
                    raise RunConflict('Send a new chat message to change a completed plan.')
                run.steering = [*run.steering, {'id': str(uuid.uuid4()), 'message': message, 'status': 'queued'}]
                run.revision += 1
            db.commit()
        self.event(run_id, 'run', self.snapshot(owner, run_id))
        return self.snapshot(owner, run_id)

    def take_steering(self, run_id):
        with self.sessions() as db:
            run = db.query(PlanningRun).filter_by(run_id=run_id).with_for_update().one()
            queued = [item for item in run.steering if item['status'] == 'queued']
            run.steering = [{**item, 'status': 'applied'} for item in run.steering]
            if run.control == 'interrupt':
                run.control = None
            db.commit()
            return queued


store = RunStore()
