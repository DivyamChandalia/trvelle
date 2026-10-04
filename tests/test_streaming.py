"""Long research should keep the proxy alive without duplicating work."""
import asyncio
import unittest
from trvelle.orchestrator.streaming import with_keepalive


class KeepaliveTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_research_emits_comments_and_preserves_output(self):
        calls = []
        async def source():
            calls.append('started')
            yield 'event: progress\ndata: "Researching"\n\n'
            await asyncio.sleep(.04)
            yield 'event: done\ndata: ""\n\n'
        chunks = [chunk async for chunk in with_keepalive(source(), interval=.005)]
        self.assertEqual(calls, ['started'])
        self.assertIn(': keepalive\n\n', chunks)
        self.assertEqual(chunks[0], 'event: progress\ndata: "Researching"\n\n')
        self.assertEqual(chunks[-1], 'event: done\ndata: ""\n\n')

    async def test_disconnect_cancels_pending_research_and_closes_source(self):
        closed = asyncio.Event()
        async def source():
            try:
                await asyncio.Event().wait()
                yield 'never'
            finally:
                closed.set()
        stream = with_keepalive(source(), interval=.005)
        self.assertEqual(await anext(stream), ': keepalive\n\n')
        await stream.aclose()
        self.assertTrue(closed.is_set())

    async def test_source_error_propagates(self):
        async def source():
            yield 'started'
            raise ValueError('Research failed')
        stream = with_keepalive(source(), interval=.005)
        self.assertEqual(await anext(stream), 'started')
        with self.assertRaisesRegex(ValueError, 'Research failed'):
            await anext(stream)
