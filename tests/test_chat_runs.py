"""Connection lifetimes must not determine planning task lifetimes."""
import asyncio
import uuid
import unittest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from unittest.mock import Mock, AsyncMock
from trvelle.orchestrator.chat_runs import ChatRuns
from trvelle.orchestrator.client import Orchestrator

class ChatRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_continues_same_task_and_owner_is_isolated(self):
        runs = ChatRuns()
        release = asyncio.Event()
        started = []
        async def source():
            started.append('started')
            yield 'first'
            await release.wait()
            yield 'second'
        owner, chat = uuid.uuid4(), uuid.uuid4()
        run = runs.start(owner, chat, source())
        subscriber = run.subscribe()
        self.assertEqual(await anext(subscriber), 'first')
        await subscriber.aclose()
        self.assertFalse(run.task.done())
        self.assertIsNone(runs.get(uuid.uuid4(), chat))
        reconnect = run.subscribe(after=1)
        release.set()
        self.assertEqual(await anext(reconnect), 'second')
        self.assertEqual([event async for event in reconnect], [])
        self.assertEqual(started, ['started'])
        await runs.close()

    async def test_shutdown_cancels_work_and_closes_source(self):
        runs = ChatRuns()
        closed = asyncio.Event()
        async def source():
            try:
                yield 'first'
                await asyncio.Event().wait()
            finally:
                closed.set()
        run = runs.start('owner', 'chat', source())
        subscriber = run.subscribe()
        await anext(subscriber)
        await runs.close()
        self.assertTrue(closed.is_set())
        await subscriber.aclose()

    async def test_pending_calls_skip_completed_searches(self):
        agent = object.__new__(Orchestrator)
        agent.chat_manager = Mock()
        original = AIMessage(content='', tool_calls=[{'name':'hotel_search','args':{},'id':'hotel','type':'tool_call'}, {'name':'tavily_search','args':{},'id':'activity','type':'tool_call'}])
        agent.chat_manager.get_chat_history.return_value = [HumanMessage(content='Plan Singapore'), original, ToolMessage(content='Hotels', tool_call_id='hotel')]
        pending = agent.pending_response({'user_id':'owner','chat_id':'chat'})
        self.assertEqual([call['id'] for call in pending.tool_calls], ['activity'])
        self.assertEqual(len(original.tool_calls), 2)

    async def test_completed_research_segment_is_reused_without_model_or_tool_calls(self):
        agent = object.__new__(Orchestrator)
        query = 'Plan this trip segment'
        segment = ToolMessage(content='Saved plan', name='trip_segment', tool_call_id='segment')
        agent.chat_manager = Mock()
        agent.chat_manager.get_chat_history.return_value = [HumanMessage(content=query), segment]
        agent.load_chat_history = Mock()
        agent.get_message = Mock()
        agent.call_researcher_llm = AsyncMock()
        self.assertIs(await agent.orchestrate_research(query, {'user_id':'owner','chat_id':'chat','segment_number':1}), segment)
        agent.get_message.assert_not_called()
        agent.call_researcher_llm.assert_not_awaited()

    async def test_segment_identity_survives_database_argument_reordering(self):
        self.assertTrue(Orchestrator.same_research_query("Plan this trip segment: {'city': 'Singapore', 'segment_number': 1}", "Plan this trip segment: {'segment_number': 1, 'city': 'Singapore'}"))
        self.assertFalse(Orchestrator.same_research_query("Plan this trip segment: {'city': 'Singapore'}", "Plan this trip segment: {'city': 'Kyoto'}"))
