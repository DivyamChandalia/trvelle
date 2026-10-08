import json,unittest,uuid
from unittest.mock import AsyncMock,Mock,patch
from langchain_core.messages import AIMessage
from trvelle.orchestrator.client import Orchestrator
from trvelle.orchestrator.search_budget_notice import blocked_search_tools,budget_notice
from trvelle.tools.search_gateway import SearchBudgetError,search_context

class BudgetNoticeTests(unittest.IsolatedAsyncioTestCase):
 def test_only_exhausted_search_providers_are_disabled(self):
  state={'search_limits':{'serpapi':6,'brave':20,'tavily':6},'search_usage':{'serpapi':6,'brave':2,'tavily':6}}
  self.assertEqual(blocked_search_tools(state),{'flight_search','hotel_search'})
  state['search_usage']['brave']=20
  self.assertEqual(blocked_search_tools(state),{'flight_search','hotel_search','web_search','tavily_search','brave_place_search'})
  self.assertIn('saved search results',json.loads(budget_notice(['serpapi']))['instruction'])
 async def test_wrapped_budget_error_is_a_successful_tool_notice(self):
  agent=object.__new__(Orchestrator);agent.checkpoint=AsyncMock()
  async def blocked(*args):
   try:raise SearchBudgetError('This task reached its tavily budget (1).')
   except SearchBudgetError as e:raise RuntimeError('Wrapped tool failure') from e
  tool=Mock(ainvoke=AsyncMock(side_effect=blocked))
  agent.get_tools=AsyncMock(return_value=([tool],{'tavily_search':tool}))
  call={'name':'tavily_search','id':'budget-notice','args':{'query':'Museum hours'},'type':'tool_call'}
  with search_context(providers={'web':'tavily'}):messages=await agent.handle_tools([call],'system')
  self.assertEqual(messages[0].status,'success')
  self.assertEqual(json.loads(messages[0].content)['status'],'budget_exhausted')
 async def test_exhausted_tools_are_not_called_or_saved_as_inventory(self):
  agent=object.__new__(Orchestrator);agent.checkpoint=AsyncMock();agent.chat_manager=Mock()
  tool=Mock(ainvoke=AsyncMock());agent.get_tools=AsyncMock(return_value=([tool],{'tavily_search':tool}))
  config={'run_id':uuid.uuid4(),'user_id':uuid.uuid4(),'chat_id':uuid.uuid4()}
  budget={'search_limits':{'brave':1,'tavily':1},'search_usage':{'brave':1,'tavily':1}}
  with patch('trvelle.orchestrator.run_store.store.snapshot',return_value=budget),patch('trvelle.orchestrator.run_store.store.operation'):
   messages,raw=await Orchestrator.handle_tools.__wrapped__(agent,[{'name':'tavily_search','id':'budget-fixture','args':{'query':'Museum hours'}}],'system',config)
  tool.ainvoke.assert_not_awaited();self.assertEqual(raw,[None]);self.assertEqual(messages[0].status,'success')
 async def test_model_gets_budget_notice_and_only_remaining_tools(self):
  agent=object.__new__(Orchestrator);agent.db_handler=Mock()
  reply=AIMessage(content='Draft ready',additional_kwargs={'routing_provider':'openrouter','routing_model':'openrouter/free'})
  agent.model_router=Mock(invoke=AsyncMock(return_value=reply))
  flight=Mock();flight.name='flight_search';publish=Mock();publish.name='itinerary_tool'
  config={'run_id':uuid.uuid4(),'user_id':uuid.uuid4()}
  budget={'search_limits':{'serpapi':6},'search_usage':{'serpapi':6},'models':{}}
  with patch('trvelle.orchestrator.run_store.store.snapshot',return_value=budget),patch('trvelle.orchestrator.run_store.store.reserve_model',return_value='model-fixture'),patch('trvelle.orchestrator.run_store.store.operation'),patch('trvelle.orchestrator.run_store.store.checkpoint'):
   await agent.invoke_model(config,'supervisor',[flight,publish],[])
  call=agent.model_router.invoke.call_args
  self.assertEqual(call.args[1],[publish]);self.assertIn('saved search results',call.args[2][-1].content)
