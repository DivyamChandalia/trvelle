"""Conversation version navigation, ownership and inference-context isolation."""
import asyncio, os, unittest, uuid
from unittest.mock import Mock, patch
from datetime import datetime, timedelta, timezone
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from fastapi.testclient import TestClient
from trvelle.database.models import User, ChatSession, Message, ToolExecution, PlanningRun
from trvelle.database.chat_versions import version
from trvelle.orchestrator.api_wrapper import app, db_handler
from trvelle.orchestrator.run_store import store, RunConflict


class ChatVersionTests(unittest.TestCase):
    def setUp(self):
        self.owner, self.other, self.chat = [uuid.uuid4() for _ in range(3)]
        with db_handler.db_session() as db:
            db.add_all([User(user_id=self.owner), User(user_id=self.other)])
            db.flush()
            db.add(ChatSession(chat_id=self.chat, user_id=self.owner))
            db.commit()
        self.client = TestClient(app)
        self.headers = {'user-id':str(self.owner), 'x-backend-token':os.environ['BACKEND_API_TOKEN']}

    def tearDown(self):
        with db_handler.db_session() as db:
            for owner in (self.owner, self.other):
                db.delete(db.get(User, owner))
            db.commit()
        self.client.close()

    def turn(self, text, replace=None, plan=False, status='complete', hotel=None):
        run = store.create(self.owner, self.chat, {'message':text, 'currency':'INR', 'choices':{},
            'search_limits':{'serpapi':6,'tavily':6,'brave':20}, 'replace_message_id':str(replace) if replace else None})
        run_id = uuid.UUID(run['run_id'])
        config = {'user_id':self.owner, 'chat_id':self.chat, 'run_id':run_id, 'replace_message_id':str(replace) if replace else None}
        if replace:
            db_handler.rewind_chat_turn(self.owner, self.chat, replace)
        human_id = uuid.uuid4()
        db_handler.save_message_to_db(HumanMessage(content=text, id=str(human_id)), config)
        if hotel:
            identifier = uuid.uuid4()
            db_handler.save_message_to_db(ToolMessage(content=hotel, tool_call_id=str(identifier), name='hotel_search'), config)
            with db_handler.db_session() as db:
                db.add(ToolExecution(message_id=identifier, chat_id=self.chat, tool_name='hotel_search', raw_response={
                    'search_parameters':{'q':'Rome','currency':'INR'}, 'properties':[{'name':hotel, 'choose_uid':str(identifier)}]}))
                db.commit()
        itinerary_id = None
        if plan:
            itinerary_id = uuid.uuid4()
            db_handler.save_message_to_db(ToolMessage(content='Saved itinerary', tool_call_id=str(itinerary_id), name='itinerary_tool'), config)
            with db_handler.db_session() as db:
                searches = db.query(ToolExecution).filter_by(chat_id=self.chat, tool_name='hotel_search').all()
                raw = {'trip_name':text+' plan', 'daily_plan':[], 'planning_status':'partial' if status=='partial' else 'complete',
                    'unfinished':['Room verification'] if status=='partial' else [], 'source_search_ids':[str(row.message_id) for row in searches]}
                db.add(ToolExecution(message_id=itinerary_id, chat_id=self.chat, tool_name='itinerary_tool', raw_response=raw))
                db.commit()
            store.checkpoint(run_id, 'publish', itinerary_id=str(itinerary_id))
        db_handler.save_message_to_db(AIMessage(content='Reply to '+text, id=str(uuid.uuid4()), name='Supervisor_Agent'), config)
        store.started(run_id)
        store.finish(run_id, status)
        return human_id, run_id, itinerary_id

    def choose(self, identifier):
        response = self.client.post('/chat_version', params={'chat_id':str(self.chat)}, headers=self.headers, json={'message_id':str(identifier)})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_retry_and_edit_versions_keep_their_own_response_and_itinerary(self):
        first, _, first_plan = self.turn('Rome', plan=True)
        second, _, second_plan = self.turn('Florence', replace=first, plan=True)
        third, _, _ = self.turn('Florence', replace=second, plan=True)
        history = db_handler.get_filtered_chat_history(self.owner, self.chat)
        self.assertEqual(history[0]['metadata']['versions']['current'], 3)
        self.assertEqual(history[0]['metadata']['versions']['total'], 3)
        self.assertEqual(history[-1]['metadata']['versions']['current'], 3)
        old = self.choose(first)['messages']
        self.assertEqual(old[0]['content'], 'Rome')
        self.assertEqual(old[-1]['content'], 'Reply to Rome')
        self.assertEqual(old[1]['metadata']['itineraryId'], str(first_plan))
        newer = self.choose(second)['messages']
        self.assertEqual(newer[1]['metadata']['itineraryId'], str(second_plan))
        self.assertEqual(newer[0]['metadata']['versions']['current'], 2)
        self.choose(third)
        with db_handler.db_session() as db:
            self.assertEqual(db.query(Message).filter_by(chat_id=self.chat, type='human').count(), 3)

    def test_switching_restores_follow_up_branches_and_their_selected_revisions(self):
        first, _, _ = self.turn('Rome')
        follow_up, _, _ = self.turn('Add a museum')
        second, _, _ = self.turn('Florence', replace=first)
        self.turn('Add a viewpoint')
        old = self.choose(first)
        self.assertEqual([row['content'] for row in old['messages'] if row['type']=='human'], ['Rome','Add a museum'])
        edited_follow_up, _, _ = self.turn('Add two museums', replace=follow_up)
        self.assertEqual([m.content for m in db_handler.load_chat_history(self.owner,self.chat)],
            ['Rome','Reply to Rome','Add two museums','Reply to Add two museums'])
        other = self.choose(second)
        self.assertEqual([row['content'] for row in other['messages'] if row['type']=='human'], ['Florence','Add a viewpoint'])
        restored = self.choose(first)
        self.assertEqual(restored['messages'][-1]['content'], 'Reply to Add two museums')
        self.assertEqual(restored['messages'][-2]['message_id'], str(edited_follow_up))

    def test_editing_an_older_version_creates_another_sibling(self):
        first, _, _ = self.turn('Rome')
        second, _, _ = self.turn('Florence', replace=first)
        self.choose(first)
        third, _, _ = self.turn('Venice', replace=first)
        history = db_handler.get_filtered_chat_history(self.owner,self.chat)
        self.assertEqual(history[0]['metadata']['versions']['message_ids'], list(map(str,[first,second,third])))
        self.assertEqual(history[0]['metadata']['versions']['current'], 3)

    def test_quotes_from_sibling_branches_are_excluded_from_hotel_alternatives(self):
        first, _, first_plan = self.turn('Rome', plan=True, hotel='Original hotel')
        _, _, next_plan = self.turn('Florence', replace=first, plan=True, hotel='New hotel')
        old = db_handler.get_itinerary(self.owner,self.chat,first_plan)
        new = db_handler.get_itinerary(self.owner,self.chat,next_plan)
        self.assertEqual([hotel['name'] for hotel in old['travel_options']['hotels']], ['Original hotel'])
        self.assertEqual([hotel['name'] for hotel in new['travel_options']['hotels']], ['New hotel'])
        old_uid=old['travel_options']['hotels'][0]['choose_uid']
        self.assertIn('exact choose_uid', db_handler.itinerary_search_issue(self.owner,self.chat,
            {'daily_plan':[{'items':[{'card_type':'hotel','uid':old_uid}]}]}))

    def test_run_status_and_continuation_follow_the_selected_version(self):
        first, first_run, _ = self.turn('Rome', plan=True, status='partial')
        _, second_run, _ = self.turn('Florence', replace=first, plan=True)
        selected = self.choose(first)
        self.assertEqual(selected['run']['run_id'], str(first_run))
        self.assertTrue(selected['run']['can_resume'])
        self.assertEqual(store.snapshot(self.owner,chat_id=self.chat)['run_id'], str(first_run))
        with self.assertRaises(RunConflict):
            store.control(self.owner,second_run,'resume')

    def test_wrong_owner_cannot_read_or_switch_versions(self):
        first, _, _ = self.turn('Rome')
        response = self.client.post('/chat_version', params={'chat_id':str(self.chat)},
            headers={**self.headers,'user-id':str(self.other)}, json={'message_id':str(first)})
        self.assertEqual(response.status_code,404)
        self.assertEqual(self.client.post('/chat_version', params={'chat_id':str(self.chat)},headers=self.headers,
            json={'message_id':str(uuid.uuid4())}).status_code,404)

    def test_version_switching_is_blocked_while_planning(self):
        first, _, _ = self.turn('Rome')
        store.create(self.owner,self.chat,{'message':'Research more','currency':'INR','choices':{},'search_limits':{}})
        response = self.client.post('/chat_version',params={'chat_id':str(self.chat)},headers=self.headers,json={'message_id':str(first)})
        self.assertEqual(response.status_code,409)
        self.assertFalse(db_handler.get_filtered_chat_history(self.owner,self.chat)[0].get('superseded',False))

    def test_hidden_prompt_cannot_be_retried_without_selecting_it(self):
        first, _, _ = self.turn('Rome')
        self.turn('Florence',replace=first)
        response = self.client.post('/runs/start',params={'chat_id':str(self.chat)},headers=self.headers,
            json={'message':'Rome','replace_message_id':str(first)})
        self.assertEqual(response.status_code,404)

    def test_existing_retry_records_are_backfilled_without_losing_content(self):
        now = datetime.now(timezone.utc)
        original, retry, old_run, new_run = [uuid.uuid4() for _ in range(4)]
        with db_handler.db_session() as db:
            db.add_all([
                PlanningRun(run_id=old_run,chat_id=self.chat,user_id=self.owner,status='complete',request={},checkpoint={},counters={},created_at=now-timedelta(minutes=4)),
                PlanningRun(run_id=new_run,chat_id=self.chat,user_id=self.owner,status='complete',request={'replace_message_id':str(original)},checkpoint={},counters={},created_at=now-timedelta(minutes=2)),
                Message(message_id=original,chat_id=self.chat,user_id=self.owner,type='human',content='Old prompt',superseded=True,created_at=now-timedelta(minutes=3)),
                Message(message_id=retry,chat_id=self.chat,user_id=self.owner,type='human',content='Edited prompt',created_at=now-timedelta(minutes=1))])
            db.commit()
        history = db_handler.get_filtered_chat_history(self.owner,self.chat)
        self.assertEqual(history[0]['content'],'Edited prompt')
        self.assertEqual(history[0]['metadata']['versions']['total'],2)
        self.assertEqual(self.choose(original)['messages'][0]['content'],'Old prompt')
        with db_handler.db_session() as db:
            self.assertEqual(version(db.get(Message,retry))['root_id'],str(original))

    def test_exhausted_planning_budget_does_not_offer_a_futile_resume(self):
        _, run_id, _ = self.turn('Rome',plan=True,status='partial')
        with db_handler.db_session() as db:
            run=db.get(PlanningRun,run_id)
            run.counters={'supervisor':8}
            db.commit()
        self.assertFalse(store.snapshot(self.owner,run_id)['can_resume'])

    def test_rewind_is_idempotent_before_a_replacement_prompt_is_saved(self):
        first, _, _ = self.turn('Rome')
        db_handler.rewind_chat_turn(self.owner,self.chat,first)
        db_handler.rewind_chat_turn(self.owner,self.chat,first)
        self.assertEqual(db_handler.get_filtered_chat_history(self.owner,self.chat),[])

    def test_recovery_does_not_rewind_an_already_saved_replacement_prompt(self):
        from trvelle.orchestrator.worker import perform
        first, _, _ = self.turn('Rome')
        run=store.create(self.owner,self.chat,{'message':'Florence','currency':'INR','choices':{},'search_limits':{},'replace_message_id':str(first)})
        config={'user_id':self.owner,'chat_id':self.chat,'run_id':uuid.UUID(run['run_id']),'query':'Florence','resume':False,
            'choices':{},'search_limits':{},'replace_message_id':str(first)}
        db_handler.rewind_chat_turn(self.owner,self.chat,first)
        db_handler.save_message_to_db(HumanMessage(content='Florence',id=str(uuid.uuid4())),config)
        class Agent:
            load_chat_history=Mock()
            get_message=Mock(side_effect=AssertionError('Prompt was already saved'))
            async def orchestrate_stream(self,*args,**kwargs):
                if False:yield None
        agent=Agent();agent.db_handler=db_handler
        with patch('trvelle.orchestrator.worker.get_accounts') as accounts:
            accounts.return_value.load.return_value={'roles':{}}
            asyncio.run(perform(agent,config))
        self.assertEqual(db_handler.get_filtered_chat_history(self.owner,self.chat)[0]['content'],'Florence')
