import unittest,uuid
from unittest.mock import Mock,patch,AsyncMock
from trvelle.orchestrator.worker import perform
from trvelle.orchestrator.run_store import RunConflict


class WorkerCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_continuation_prompt_requires_real_update_and_blocks_false_publication_claim(self):
        from langchain_core.messages import AIMessage,SystemMessage
        from trvelle.orchestrator.client import Orchestrator
        agent=object.__new__(Orchestrator)
        agent.chat_manager=Mock();agent.chat_manager.get_chat_history.return_value=[]
        agent.checkpoint=AsyncMock();agent.get_tools=AsyncMock(return_value=([],{}))
        agent.invoke_model=AsyncMock(return_value=AIMessage(content='Published your complete itinerary!',additional_kwargs={'routing_tier':1}))
        config={'user_id':uuid.uuid4(),'chat_id':uuid.uuid4(),'currency':'INR','continuation_plan':{
            'unfinished':['Final night hotel'],'travel_options':{'hotels':[{'name':'Real hotel','choose_uid':'real-uid','total_rate':{'lowest':'INR 5000'}}]}}}
        response=await Orchestrator.call_supervisor_llm.__wrapped__(agent,config)
        self.assertIn('draft is unchanged',response.content)
        prompt=agent.invoke_model.call_args.args[3]
        instruction='\n'.join(message.content for message in prompt.messages if isinstance(message,SystemMessage))
        self.assertIn('explicitly continued',instruction)
        self.assertIn('itinerary_tool',instruction)
        self.assertIn('real-uid',instruction)

    async def test_resume_without_new_publication_keeps_existing_draft_partial(self):
        identifier=str(uuid.uuid4())
        class Agent:
            db_handler=Mock()
            load_chat_history=Mock()
            async def orchestrate_stream(self,*args,**kwargs):
                yield {'message':'The saved draft is unchanged'}
        agent=Agent();agent.db_handler.get_itinerary.return_value={'planning_status':'partial','unfinished':['Final hotel']}
        config={'user_id':uuid.uuid4(),'chat_id':uuid.uuid4(),'run_id':uuid.uuid4(),
                'currency':'INR','query':'Plan Italy','resume':True,'choices':{},'search_limits':{},'existing_itinerary_id':identifier}
        with patch('trvelle.orchestrator.worker.store') as store,patch('trvelle.orchestrator.worker.get_accounts') as accounts:
            accounts.return_value.load.return_value={'roles':{}}
            await perform(agent,config)
            store.finish.assert_called_once_with(config['run_id'],'partial')
            self.assertEqual(config['continuation_plan']['unfinished'],['Final hotel'])

    async def test_partial_publication_is_resumable_instead_of_complete(self):
        identifier=str(uuid.uuid4())
        class Agent:
            db_handler=Mock()
            load_chat_history=Mock()
            async def orchestrate_stream(self,*args,**kwargs):
                yield {'itinerary':identifier}
        agent=Agent();agent.db_handler.get_itinerary.return_value={'planning_status':'partial'}
        config={'user_id':uuid.uuid4(),'chat_id':uuid.uuid4(),'run_id':uuid.uuid4(),
                'currency':'INR','query':'Plan Italy','resume':True,'choices':{},'search_limits':{}}
        with patch('trvelle.orchestrator.worker.store') as store,patch('trvelle.orchestrator.worker.get_accounts') as accounts:
            accounts.return_value.load.return_value={'roles':{}}
            await perform(agent,config)
            store.finish.assert_called_once_with(config['run_id'],'partial')

    async def test_rejected_publication_cannot_finish_as_successful_chat_only_output(self):
        class Agent:
            load_chat_history=Mock()
            async def orchestrate_stream(self,*args,**kwargs):
                yield {'message':'Unable to publish the plan'}
        config={'user_id':uuid.uuid4(),'chat_id':uuid.uuid4(),'run_id':uuid.uuid4(),
                'currency':'INR','query':'Plan Italy','resume':True,'choices':{},'search_limits':{},'publication_attempted':True}
        with patch('trvelle.orchestrator.worker.store') as store,patch('trvelle.orchestrator.worker.get_accounts') as accounts:
            accounts.return_value.load.return_value={'roles':{}}
            with self.assertRaises(RunConflict):await perform(Agent(),config)
            store.finish.assert_not_called()
