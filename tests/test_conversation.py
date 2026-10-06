"""Read-only follow-ups, single reply persistence and legacy duplicate display."""
import unittest
import uuid
from types import SimpleNamespace, MethodType
from unittest.mock import AsyncMock, Mock, patch
from langchain_core.messages import AIMessage, HumanMessage
from trvelle.database.db_handler import DBHandler
from trvelle.database.models import User, ChatSession, Message, ToolExecution, PlanningRun
from trvelle.orchestrator.run_store import store
from trvelle.orchestrator.run_api import start
from trvelle.orchestrator.api_wrapper import ChatRequest
from trvelle.orchestrator.client import Orchestrator
from trvelle.orchestrator.worker import perform
from trvelle.utils.message_intent import message_mode


class IntentTests(unittest.TestCase):
    def test_questions_are_not_inventory_requests(self):
        for text in ['how long will the flight be from mumbai to tokyo', 'What is the hotel price?', 'Can you explain the flight layover?', 'How can I change the hotel?', 'Why did you choose Tokyo?', 'I want to know the total price', 'Tell me what food I like', 'Thanks!']:
            with self.subTest(text=text):
                self.assertEqual(message_mode(text,has_plan=True),'chat')
        for text in ['Can you add restaurants?', 'Move the museum to 11:00', 'Please change the flight', 'Recommend local food places', 'Complete only the missing overnight stay', 'Please can you add a museum?']:
            self.assertEqual(message_mode(text,has_plan=True),'update')
        self.assertEqual(message_mode('Plan a trip to Italy',has_plan=True),'plan')
        self.assertEqual(message_mode('hello'),'chat')
        self.assertEqual(message_mode('6 people, moderate pace',continuing_plan=True),'plan')


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owner,self.chat,self.itinerary=[uuid.uuid4() for _ in range(3)]
        self.plan={'trip_name':'Saved Tokyo trip','revision':1,'summary':{'origin':'Mumbai','travelers':6,'currency':'INR','dates':{'start':'2027-04-25','end':'2027-04-25'}},'source_search_ids':[], 'daily_plan':[{'day':1,'date':'2027-04-25','destination':'Tokyo','items':[{'item_id':'museum','card_type':'activity','title':'Museum','start_time':'09:00','end_time':'11:00'}]}]}
        with DBHandler.db_session() as db:
            db.add(User(user_id=self.owner));db.flush()
            db.add(ChatSession(chat_id=self.chat,user_id=self.owner));db.flush()
            db.add(Message(message_id=self.itinerary,user_id=self.owner,chat_id=self.chat,type='tool',message_name='itinerary_tool',content='Saved plan'))
            db.add(ToolExecution(message_id=self.itinerary,chat_id=self.chat,tool_name='itinerary_tool',raw_response=self.plan));db.commit()

    def tearDown(self):
        with DBHandler.db_session() as db:
            db.delete(db.get(User,self.owner));db.commit()

    async def test_question_has_no_search_budget_and_keeps_manual_edit_context(self):
        from copy import deepcopy
        edited=deepcopy(self.plan);edited['daily_plan'][0]['items'][0]['start_time']='09:30'
        DBHandler().save_itinerary_edit(self.owner,self.chat,self.itinerary,edited,expected_revision=1)
        run=await start(ChatRequest(message='When does the museum visit start?'),self.owner,self.chat)
        with DBHandler.db_session() as db:
            request=db.get(PlanningRun,uuid.UUID(run['run_id'])).request
        self.assertEqual(run['mode'],'chat')
        self.assertEqual(request['search_limits'],{'serpapi':0,'tavily':0,'brave':0})
        self.assertEqual(request['base_revision'],2)
        self.assertIn({'op':'replace','path':'/daily_plan/0/items/0/start_time','value':'09:30'},request['context_diff'])
        config={**request,'query':request['message'],'run_id':uuid.UUID(run['run_id']),'user_id':self.owner,'chat_id':self.chat,'resume':False}
        router=SimpleNamespace(invoke=AsyncMock(return_value=AIMessage(content='The museum visit starts at 09:30.',id=str(uuid.uuid4()))))
        agent=SimpleNamespace(db_handler=DBHandler(),model_router=router,load_chat_history=Mock())
        agent.invoke_model=MethodType(Orchestrator.invoke_model,agent)
        agent.get_message=lambda text,c: agent.db_handler.save_message_to_db(HumanMessage(content=text,id=str(uuid.uuid4())),c)
        with patch('trvelle.orchestrator.worker.get_accounts',return_value=SimpleNamespace(load=lambda _:{})), patch('trvelle.tools.search_gateway.gateway.request',side_effect=AssertionError('Question must not search')):
            await perform(agent,config)
            config['resume']=True
            await perform(agent,config)
        router.invoke.assert_awaited_once()
        args=router.invoke.call_args.args
        self.assertEqual(args[1],[])
        self.assertIn('09:30',args[2][-1].content)
        self.assertEqual(DBHandler().get_itinerary(self.owner,self.chat,self.itinerary)['revision'],2)
        with DBHandler.db_session() as db:
            replies=db.query(Message).filter_by(chat_id=self.chat,type='ai',message_name='Supervisor_Agent').all()
            self.assertEqual(len(replies),1)
            self.assertEqual(replies[0].content,'The museum visit starts at 09:30.')

    async def test_agent_edit_has_one_persistent_version_link_and_legacy_updates_restore_it(self):
        from copy import deepcopy
        request={'message':'Move the museum later','currency':'INR','mode':'update','update_scope':'general','base_itinerary_id':str(self.itinerary),'base_revision':1,'search_limits':{'serpapi':0,'tavily':0,'brave':0}}
        run=store.create(self.owner,self.chat,request)
        config={**request,'query':request['message'],'run_id':uuid.UUID(run['run_id']),'user_id':self.owner,'chat_id':self.chat,'resume':False}
        edited=deepcopy(self.plan);edited['daily_plan'][0]['items'][0]['start_time']='10:00'
        edited['applied_update_runs']={run['run_id']:2}
        DBHandler().save_itinerary_edit(self.owner,self.chat,self.itinerary,edited,expected_revision=1)
        agent=SimpleNamespace(db_handler=DBHandler(),load_chat_history=Mock())
        agent.get_message=lambda text,c: agent.db_handler.save_message_to_db(HumanMessage(content=text,id=str(uuid.uuid4())),c)
        with patch('trvelle.orchestrator.worker.get_accounts',return_value=SimpleNamespace(load=lambda _:{})), patch('trvelle.orchestrator.itinerary_updates.perform_update',new=AsyncMock(return_value=(str(self.itinerary),'Moved the museum. Saved as version 2.'))):
            await perform(agent,config)
            config['resume']=True
            await perform(agent,config)
        history=DBHandler().get_filtered_chat_history(self.owner,self.chat)
        reply=next(row for row in history if row['type']=='ai')
        self.assertEqual(reply['metadata']['itineraryId'],str(self.itinerary))
        self.assertEqual(reply['metadata']['itineraryRevision'],2)
        self.assertTrue(reply['metadata']['itineraryUpdated'])
        with DBHandler.db_session() as db:
            rows=db.query(Message).filter_by(chat_id=self.chat,type='ai').all()
            self.assertEqual(len(rows),1)
            rows[0].additional_kwargs={key:value for key,value in rows[0].additional_kwargs.items() if key!='itinerary_ref'}
            db.commit()
        legacy=next(row for row in DBHandler().get_filtered_chat_history(self.owner,self.chat) if row['type']=='ai')
        self.assertEqual(legacy['metadata']['itineraryRevision'],2)
        self.assertEqual(legacy['metadata']['itineraryId'],str(self.itinerary))

    async def test_old_duplicate_reply_is_hidden_without_merging_user_turns(self):
        handler=DBHandler()
        def add_turn():
            run=store.create(self.owner,self.chat,{'message':'How long is the flight?','currency':'INR','search_limits':{},'mode':'chat'})
            config={'user_id':self.owner,'chat_id':self.chat,'run_id':uuid.UUID(run['run_id'])}
            handler.save_message_to_db(HumanMessage(content='How long is the flight?',id=str(uuid.uuid4())),config)
            handler.save_message_to_db(AIMessage(content='8 hours 25 minutes.',id=str(uuid.uuid4()),name='Supervisor_Agent'),config)
            handler.save_message_to_db(AIMessage(content='8 hours 25 minutes.\nYour saved itinerary is unchanged.',id=str(uuid.uuid4()),name='Supervisor_Agent'),config)
            store.finish(config['run_id'],'complete')
        add_turn();add_turn()
        replies=[row for row in handler.get_filtered_chat_history(self.owner,self.chat) if row['type']=='ai']
        self.assertEqual([row['content'] for row in replies],['8 hours 25 minutes.','8 hours 25 minutes.'])
        with DBHandler.db_session() as db:
            self.assertEqual(db.query(Message).filter_by(chat_id=self.chat,type='ai').count(),4)
