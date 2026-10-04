import unittest
from unittest.mock import Mock, AsyncMock
from langchain_core.messages import AIMessage, ToolMessage
from trvelle.orchestrator.research_budget import counts, compact, should_finalize
from trvelle.orchestrator.client import Orchestrator

class ResearchBudgetTests(unittest.IsolatedAsyncioTestCase):
    def test_saved_searches_and_model_rounds_count_towards_finalization(self):
        history = [AIMessage(content='Research') for _ in range(4)]
        self.assertTrue(should_finalize(history))
        self.assertFalse(should_finalize(history[:3]))
        searches = [ToolMessage(content='Verified option', name=name, tool_call_id=str(i)) for name, count in [('hotel_search',3),('tavily_search',6)] for i in range(count)]
        self.assertTrue(should_finalize(searches))
        self.assertEqual(counts(searches), {'hotel_search':3,'tavily_search':6})

    def test_prompt_compaction_preserves_original_evidence_and_tool_ids(self):
        calls = AIMessage(content='',tool_calls=[{'name':'hotel_search','args':{},'id':'hotel','type':'tool_call'}])
        result = ToolMessage(content='Verified hotel UID abc, INR 8000\n'+'Details '*2000,name='hotel_search',tool_call_id='hotel')
        shortened = compact([calls,result])
        self.assertIs(shortened[0],calls)
        self.assertEqual(shortened[1].tool_call_id,'hotel')
        self.assertIn('Verified hotel UID abc, INR 8000',shortened[1].content)
        self.assertLess(len(shortened[1].content),5200)
        self.assertGreater(len(result.content),10000)

    def test_old_hotel_results_keep_all_property_quote_uid_pairs(self):
        content='Found 3 hotel(s):\n'
        for number in range(1,4):
            content+=f'**Hotel {number}: Property {number}**\nThe UID to choose this hotel is: "hotel-{number}"\nDescription: '+('Long description '*700)+f'\nTotal rate: INR {number*1000}\n'
        history=[ToolMessage(content=content,name='hotel_search',tool_call_id='hotels')]
        history += [ToolMessage(content='Other research',name='web_search',tool_call_id=str(i)) for i in range(4)]
        shortened=compact(history)[0].content
        for number in range(1,4):
            self.assertIn(f'Property {number}',shortened)
            self.assertIn(f'"hotel-{number}"',shortened)
            self.assertIn(f'Total rate: INR {number*1000}',shortened)
        self.assertLess(len(shortened),1000)
        self.assertEqual(history[0].content,content)

    async def test_exhausted_saved_search_budget_prevents_new_api_call(self):
        agent = object.__new__(Orchestrator)
        hotel = Mock(ainvoke=AsyncMock())
        agent.get_tools = AsyncMock(return_value=([hotel],{'hotel_search':hotel}))
        agent.chat_manager = Mock()
        agent.chat_manager.get_chat_history.return_value = [ToolMessage(content='Saved hotel',name='hotel_search',tool_call_id=str(i)) for i in range(3)]
        # Use the undecorated handler to check API work and in-memory updates;
        # no database fixture is needed for this pure budget check.
        result, raw = await Orchestrator.handle_tools.__wrapped__(agent,[{'name':'hotel_search','args':{},'id':'blocked'}],'researcher',{'user_id':'owner','chat_id':'chat','segment_number':1})
        hotel.ainvoke.assert_not_awaited()
        self.assertIn('budget reached',result[0].content)
        self.assertEqual(raw,[None])
