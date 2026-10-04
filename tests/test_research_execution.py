"""Exercise the decorated tool handler's return contract in researcher loops."""
import unittest
from unittest.mock import AsyncMock, Mock, patch
from langchain_core.messages import AIMessage, ToolMessage
from trvelle.orchestrator.client import Orchestrator
from trvelle.tools.search_gateway import search_context

class ResearchExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_web_tool_calls_honor_brave_selection(self):
        agent = object.__new__(Orchestrator)
        legacy = Mock(ainvoke=AsyncMock())
        agent.get_tools = AsyncMock(return_value=([legacy], {'tavily_search':legacy}))
        call = {'name':'tavily_search', 'args':{'query':'Uffizi Florence tickets'}, 'id':'legacy-search', 'type':'tool_call'}
        preferred = Mock(ainvoke=AsyncMock(return_value={'result':'Brave travel evidence', 'raw':{'provider':'brave'}}))
        with search_context(providers={'web':'brave'}), patch('trvelle.orchestrator.client.web_search', preferred):
            result = await agent.handle_tools([call], 'researcher')
        self.assertEqual(result[0].content,'Brave travel evidence')
        preferred.ainvoke.assert_awaited_once()
        legacy.ainvoke.assert_not_awaited()

    async def test_publishing_emits_one_completion_summary_and_card_without_an_extra_model_call(self):
        agent = object.__new__(Orchestrator)
        agent.get_message = Mock()
        agent.db_handler = Mock()
        agent.db_handler.get_message_uuid.side_effect = lambda value: value
        identifier = 'saved-plan-id'
        raw = {'summary': {'travelers': 2}, 'daily_plan': [{'destination':'Rome'}]}
        response = AIMessage(id='model-response', content='I will publish the itinerary now.', tool_calls=[{'name':'itinerary_tool','args':{'itinerary':raw},'id':identifier,'type':'tool_call'}])
        agent.call_supervisor_llm = AsyncMock(return_value=response)
        agent.handle_tools = AsyncMock(return_value=[ToolMessage(content='Itinerary displayed with UID saved-plan-id',tool_call_id=identifier,name='itinerary_tool')])
        chunks = [chunk async for chunk in agent.orchestrate_stream('Plan Rome')]
        self.assertEqual([chunk['message'] for chunk in chunks if 'message' in chunk], ['Your 1-day Rome itinerary for 2 travelers is ready.'])
        self.assertEqual(chunks[-3:], [{'message_id':identifier},{'message':'Your 1-day Rome itinerary for 2 travelers is ready.'},{'itinerary':identifier}])
        self.assertEqual(agent.call_supervisor_llm.await_count, 1)

    async def test_research_continues_after_one_two_or_three_tool_results(self):
        for count in (1, 2, 3):
            with self.subTest(tool_count=count):
                agent = object.__new__(Orchestrator)
                agent.get_message = Mock()
                tool = Mock(ainvoke=AsyncMock(return_value={'result': 'Verified travel details'}))
                agent.get_tools = AsyncMock(return_value=([tool], {'tavily_search': tool}))
                calls = [{'name': 'tavily_search', 'args': {'query': f'Singapore attraction {index}', 'max_results': 5}, 'id': f'call-{index}', 'type': 'tool_call'} for index in range(count)]
                final = AIMessage(content='Research completed')
                agent.call_researcher_llm = AsyncMock(side_effect=[AIMessage(content='', tool_calls=calls), final])
                with search_context(providers={'web':'tavily'}):
                    result = await agent.orchestrate_research('Research Singapore')
                self.assertIs(result, final)
                self.assertEqual(tool.ainvoke.await_count, count)
                self.assertEqual(agent.call_researcher_llm.await_count, 2)

    async def test_trip_segment_result_finishes_research_without_extra_model_call(self):
        agent = object.__new__(Orchestrator)
        agent.get_message = Mock()
        segment = ToolMessage(content='Completed itinerary segment', name='trip_segment', tool_call_id='segment')
        agent.handle_tools = AsyncMock(return_value=[segment])
        agent.call_researcher_llm = AsyncMock(return_value=AIMessage(content='', tool_calls=[{'name':'trip_segment','args':{},'id':'segment','type':'tool_call'}]))
        self.assertIs(await agent.orchestrate_research('Plan a segment'), segment)
        self.assertEqual(agent.call_researcher_llm.await_count, 1)
