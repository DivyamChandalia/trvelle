"""Saved-plan updates against an isolated DB and recorded model findings."""
import unittest
import uuid
from unittest.mock import patch
from langchain_core.messages import AIMessage
from trvelle.database.db_handler import DBHandler
from trvelle.database.models import User, ChatSession, Message, ToolExecution
from trvelle.orchestrator.run_store import store
from trvelle.orchestrator.itinerary_updates import perform_update


class TargetedUpdatesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owner,self.chat,self.identifier = [uuid.uuid4() for _ in range(3)]
        self.plan={'trip_name':'Saved Tokyo trip','revision':1,'summary':{'origin':'Mumbai','travelers':6,'currency':'INR','dates':{'start':'2027-04-25','end':'2027-04-25'}},'source_search_ids':[],
                   'daily_plan':[{'day':1,'date':'2027-04-25','destination':'Tokyo','items':[{'item_id':'museum','card_type':'activity','title':'Original museum','start_time':'09:00','end_time':'11:00'},
                    {'item_id':'lunch','card_type':'meal','title':'Lunch break','start_time':'12:00','end_time':'13:30'}]}]}
        with DBHandler.db_session() as db:
            db.add(User(user_id=self.owner));db.flush()
            db.add(ChatSession(chat_id=self.chat,user_id=self.owner));db.flush()
            db.add(Message(message_id=self.identifier,user_id=self.owner,chat_id=self.chat,type='tool',message_name='itinerary_tool',content='Saved plan'))
            db.add(ToolExecution(message_id=self.identifier,chat_id=self.chat,tool_name='itinerary_tool',raw_response=self.plan));db.commit()

    def tearDown(self):
        with DBHandler.db_session() as db:
            db.delete(db.get(User,self.owner));db.commit()

    async def test_followup_context_includes_manual_edits_as_a_diff_and_disables_unneeded_inventory_search(self):
        from trvelle.orchestrator.run_api import start
        from trvelle.orchestrator.api_wrapper import ChatRequest
        from trvelle.database.models import PlanningRun
        from copy import deepcopy
        edited=deepcopy(self.plan);edited['daily_plan'][0]['items'][0]['start_time']='09:30'
        DBHandler().save_itinerary_edit(self.owner,self.chat,self.identifier,edited,expected_revision=1)
        result=await start(ChatRequest(message='Move the museum visit later'),self.owner,self.chat)
        with DBHandler.db_session() as db:
            request=db.get(PlanningRun,uuid.UUID(result['run_id'])).request
        self.assertEqual(request['mode'],'update');self.assertEqual(request['update_scope'],'general')
        self.assertEqual(request['base_revision'],2)
        self.assertEqual(request['context_base']['daily_plan'][0]['items'][0]['start_time'],'09:00')
        self.assertIn({'op':'replace','path':'/daily_plan/0/items/0/start_time','value':'09:30'},request['context_diff'])
        self.assertEqual(request['search_limits']['serpapi'],0)
        store.finish(uuid.UUID(result['run_id']),'complete')
        from trvelle.database.chat_versions import KEY
        message_id=uuid.uuid4()
        with DBHandler.db_session() as db:
            db.add(Message(message_id=message_id,user_id=self.owner,chat_id=self.chat,type='human',content='Move the museum visit later',additional_kwargs={KEY:{'run_id':result['run_id'],'root_id':str(message_id),'parent_id':None}}));db.commit()
        retry=await start(ChatRequest(message='Move the museum visit to 10:00',replace_message_id=message_id),self.owner,self.chat)
        with DBHandler.db_session() as db:
            retried=db.get(PlanningRun,uuid.UUID(retry['run_id'])).request
        self.assertEqual(retried['mode'],'update')
        self.assertEqual(retried['search_limits']['serpapi'],0)

    async def test_food_update_uses_researcher_and_patch_tools_without_inventory_search_and_is_idempotent(self):
        request={'message':'Recommend local food places','currency':'INR','search_limits':{'serpapi':0,'tavily':2,'brave':8},'mode':'update','update_scope':'food','base_itinerary_id':str(self.identifier),'base_revision':1}
        run=store.create(self.owner,self.chat,request)
        config={**request,'query':request['message'],'run_id':uuid.UUID(run['run_id']),'user_id':self.owner,'chat_id':self.chat}
        patch_args={'patch':{'summary':'Added a local ramen lunch.','operations':[{'action':'replace_item','day_index':0,'item_id':'lunch','item':{'card_type':'meal','title':'Local ramen lunch','location':'Tokyo','dining':{'venue_name':'Recorded ramen shop','cuisine':'Japanese'},'cost':{'min_price':900,'max_price':1400,'currency':'INR','scope':'per_person','status':'estimate','basis':'Recorded local meal estimate.'}}}]}}
        responses=[AIMessage(content='',tool_calls=[{'id':'research','name':'research_update','args':{'city':'Tokyo','instructions':'Find food near the saved museum.'}}]),
                   AIMessage(content='Recorded ramen venue and approximate meal cost, 900–1400 INR per person.'),
                   AIMessage(content='',tool_calls=[{'id':'apply','name':'itinerary_patch','args':patch_args}])]
        calls=[]
        class Agent:
            db_handler=DBHandler()
            async def invoke_model(self,config,role,tools,prompt):
                calls.append((role,[tool.name for tool in tools]))
                return responses.pop(0)
        agent=Agent()
        with patch('trvelle.tools.search_gateway.gateway.request',side_effect=AssertionError('No provider requests expected for recorded findings')):
            identifier,text=await perform_update(agent,config)
        self.assertEqual(identifier,str(self.identifier));self.assertIn('version 2',text)
        result=DBHandler().get_itinerary(self.owner,self.chat,self.identifier)
        self.assertEqual(result['revision'],2)
        self.assertEqual(result['daily_plan'][0]['items'][0]['title'],'Original museum')
        self.assertEqual(result['daily_plan'][0]['items'][1]['title'],'Local ramen lunch')
        self.assertEqual(result['daily_plan'][0]['items'][1]['start_time'],'12:00')
        self.assertEqual([role for role,_ in calls],['supervisor','researcher','supervisor'])
        self.assertTrue(all('flight_search' not in names and 'hotel_search' not in names for _,names in calls))
        before=len(calls)
        await perform_update(agent,config)
        self.assertEqual(len(calls),before)
        self.assertEqual(DBHandler().get_itinerary(self.owner,self.chat,self.identifier)['revision'],2)
        with self.assertRaises(ValueError):
            DBHandler().save_itinerary_edit(self.owner,self.chat,self.identifier,self.plan,expected_revision=1)
        self.assertEqual(DBHandler().get_itinerary(self.owner,self.chat,self.identifier)['revision'],2)
