"""Durable runs, preserved branches and owned controls (no inference/network)."""
import os, uuid, unittest
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from trvelle.database.models import User, ChatSession, Message, ToolExecution, PlanningRun
from trvelle.orchestrator.api_wrapper import app, db_handler
from trvelle.orchestrator.run_store import store, RunConflict

class ChatActionTests(unittest.TestCase):
    def setUp(self):
        self.owner, self.other, self.chat, self.first, self.target = [uuid.uuid4() for _ in range(5)]
        now = datetime.now(timezone.utc)
        with db_handler.db_session() as db:
            db.add_all([User(user_id=self.owner), User(user_id=self.other)]); db.flush()
            db.add(ChatSession(chat_id=self.chat, user_id=self.owner)); db.flush()
            db.add_all([Message(message_id=self.first, chat_id=self.chat, user_id=self.owner, type='human', content='Prior context', created_at=now-timedelta(minutes=2)), Message(message_id=self.target, chat_id=self.chat, user_id=self.owner, type='human', content='Old request', created_at=now-timedelta(minutes=1)), ToolExecution(message_id=uuid.uuid4(), chat_id=self.chat, tool_name='hotel_search', raw_response={})]); db.commit()
        self.headers={'user-id':str(self.owner),'x-backend-token':os.environ['BACKEND_API_TOKEN']}
        self.client=TestClient(app)
    def tearDown(self):
        with db_handler.db_session() as db:
            for owner in (self.owner,self.other): db.delete(db.get(User,owner))
            db.commit()
    def start(self):
        response=self.client.post('/runs/start',params={'chat_id':str(self.chat)},headers=self.headers,json={'message':'Plan Singapore in November 2026'})
        self.assertEqual(response.status_code,200,response.text);return response.json()
    def test_edit_preserves_old_messages_and_evidence(self):
        db_handler.rewind_chat_turn(self.owner,self.chat,self.target)
        self.assertEqual([m.content for m in db_handler.load_chat_history(self.owner,self.chat)],['Prior context'])
        with db_handler.db_session() as db:
            self.assertTrue(db.get(Message,self.target).superseded)
            self.assertEqual(db.query(ToolExecution).filter_by(chat_id=self.chat).count(),1)
    def test_wrong_owner_cannot_edit(self):
        with self.assertRaises(ValueError):db_handler.rewind_chat_turn(self.other,self.chat,self.target)
    def test_one_active_run_and_owned_status(self):
        run=self.start()
        self.assertEqual(self.client.post('/runs/start',params={'chat_id':str(self.chat)},headers=self.headers,json={'message':'Another'}).status_code,409)
        self.assertEqual(self.client.get('/runs/status',params={'run_id':run['run_id']},headers={**self.headers,'user-id':str(self.other)}).status_code,404)
    def test_steering_and_stop_preserve_counters(self):
        run=self.start();identifier=uuid.UUID(run['run_id'])
        store.reserve_model(identifier,'supervisor')
        result=store.control(self.owner,identifier,'steer','Prefer a private room')
        self.assertEqual(result['queued_changes'][0]['status'],'queued')
        self.assertEqual(len(store.take_steering(identifier)),1)
        self.assertFalse(store.take_steering(identifier))
        store.control(self.owner,identifier,'stop');store.control(self.owner,identifier,'resume')
        self.assertEqual(store.snapshot(self.owner,identifier)['counters']['supervisor'],1)
    def test_model_limits_cannot_be_reset_by_resume(self):
        run=self.start();identifier=uuid.UUID(run['run_id'])
        for _ in range(8):store.reserve_model(identifier,'supervisor')
        with self.assertRaises(RunConflict):store.reserve_model(identifier,'supervisor')
        store.finish(identifier,'paused')
        with self.assertRaises(RunConflict):store.control(self.owner,identifier,'resume')
    def test_saved_completion_terminates_sse(self):
        run=self.start();identifier=uuid.UUID(run['run_id']);store.event(identifier,'message','Saved answer');store.finish(identifier,'complete')
        response=self.client.get('/runs/events',params={'run_id':str(identifier),'after':1},headers=self.headers)
        self.assertIn('Saved answer',response.text);self.assertIn('event: done',response.text)
    def test_checkpoint_survives_new_store_instance(self):
        from trvelle.orchestrator.run_store import RunStore
        run=self.start();identifier=uuid.UUID(run['run_id']);store.checkpoint(identifier,'research',task='Singapore hotels')
        self.assertEqual(RunStore().snapshot(self.owner,identifier)['task'],'Singapore hotels')

    def test_expired_worker_lease_retains_completed_operations_and_budget(self):
        run=self.start();identifier=uuid.UUID(run['run_id'])
        config=store.claim('old-worker')
        self.assertEqual(config['run_id'],identifier)
        store.started(identifier)
        store.reserve_model(identifier,'supervisor')
        store.operation(identifier,'hotel-quote','tool',{'result':'Verified saved quote'})
        with db_handler.db_session() as db:
            row=db.get(PlanningRun,identifier)
            row.lease_until=datetime.now(timezone.utc)-timedelta(seconds=1)
            db.commit()
        resumed=store.claim('replacement-worker')
        self.assertTrue(resumed['resume'])
        self.assertEqual(resumed['run_id'],identifier)
        self.assertEqual(store.operation(identifier,'hotel-quote','tool'),{'result':'Verified saved quote'})
        self.assertEqual(store.snapshot(self.owner,identifier)['counters']['supervisor'],1)

    def test_immediate_steering_does_not_terminate_the_stream(self):
        run=self.start();identifier=uuid.UUID(run['run_id'])
        store.claim('worker')
        store.control(self.owner,identifier,'steer','Use a double bed')
        store.control(self.owner,identifier,'interrupt')
        store.requeue(identifier)
        self.assertEqual(store.snapshot(self.owner,identifier)['status'],'queued')
        self.assertFalse(any(event['event']=='done' for event in store.events(self.owner,identifier)))
        self.assertEqual(store.take_steering(identifier)[0]['message'],'Use a double bed')
