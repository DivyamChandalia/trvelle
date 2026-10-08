import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from trvelle.orchestrator.model_router import ModelRouter, ModelsUnavailableError, ProviderError, reset_time, free_tool_model

class RouterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'state.sqlite3')
        with patch.dict('os.environ', {'GOOGLE_API_KEY': 'test', 'OPENROUTER_API_KEY': 'test'}):
            self.router = ModelRouter(state_path=self.path)
            self.router.role_models = {}
    def tearDown(self):
        self.router.cooldowns.db.close()
        self.temp.cleanup()
    async def test_pinned_roles_use_only_requested_models(self):
        self.router.role_models={'supervisor':['openrouter:stealth/space-bunny-alpha'],'researcher':['google:gemini-3.5-flash-lite']}
        self.assertEqual([(p,m) for p,m,t in self.router.candidates('supervisor')],[('openrouter','stealth/space-bunny-alpha')])
        self.assertEqual([(p,m) for p,m,t in self.router.candidates('researcher',3)],[('google','gemini-3.5-flash-lite')])
        self.router.request=AsyncMock(return_value=AIMessage(content='research'))
        self.router.cooldowns.block(self.router.scope('google','gemini-3.5-flash-lite'),time.time()+3600,'quota')
        with self.assertRaises(ModelsUnavailableError):
            await self.router.invoke('researcher',[],[],supervisor_tier=1)
        self.router.request.assert_not_called()
    async def test_streaming_openrouter_has_no_fixed_65_second_cutoff(self):
        self.router.role_models={'supervisor':['openrouter:openrouter/free']}
        self.router.request=AsyncMock(return_value=AIMessage(content='Streamed answer'))
        with patch('trvelle.orchestrator.model_router.asyncio.wait_for',side_effect=AssertionError('Do not truncate an active OpenRouter stream')):
            result=await self.router.invoke('supervisor',[],[])
        self.assertEqual(result.content,'Streamed answer')

    async def test_idle_timeout_is_an_app_cooldown_not_a_provider_quota_reset(self):
        self.router.role_models={'supervisor':['openrouter:openrouter/free']}
        self.router.request=AsyncMock(side_effect=__import__('httpx').ReadTimeout('Fixture stalled'))
        with self.assertRaises(ModelsUnavailableError) as caught:
            await self.router.invoke('supervisor',[],[])
        self.assertIn('not a provider quota reset',str(caught.exception))
        delay=self.router.blocked_until('openrouter','openrouter/free')-time.time()
        self.assertGreater(delay,0);self.assertLessEqual(delay,15)

    def test_reset_hints(self):
        self.assertEqual(reset_time(ProviderError(429, {}, {'Retry-After':'80','X-RateLimit-Reset':'1120'}), 1000)[0],1120)
        self.assertEqual(reset_time(ProviderError(429, {}, {'Retry-After':'Thu, 01 Jan 1970 00:20:00 GMT'}),1000)[0],1200)
        self.assertEqual(reset_time(RuntimeError("Please retry in 1h2m3.5s. {'retryDelay': '3724s'}"),1000)[0],4724)
    async def test_all_cooling_makes_zero_requests_and_persists(self):
        for p,m,_ in self.router.candidates('supervisor'):
            self.router.cooldowns.block(self.router.scope(p,m),time.time()+3600,'quota')
        self.router.request=AsyncMock()
        with self.assertRaises(ModelsUnavailableError):
            await self.router.invoke('supervisor',[],[])
        self.router.request.assert_not_called()
        with patch.dict('os.environ',{'GOOGLE_API_KEY':'test','OPENROUTER_API_KEY':'test'}):
            second=ModelRouter(state_path=self.path)
            second.role_models={}
        self.assertGreater(second.blocked_until('google','gemini-3.8-flash'),time.time())
        second.cooldowns.db.close()
    async def test_later_calls_skip_limited_model(self):
        self.router.request=AsyncMock(side_effect=[ProviderError(429,{}, {'Retry-After':'3600'}),AIMessage(content='planned'),AIMessage(content='again')])
        first=await self.router.invoke('supervisor',[],[])
        self.assertEqual(first.additional_kwargs['routing_model'],'gemini-3.8-flash')
        await self.router.invoke('supervisor',[],[])
        self.assertEqual([c.args[1] for c in self.router.request.call_args_list],['stealth/space-bunny-alpha','gemini-3.8-flash','gemini-3.8-flash'])
    def test_researchers_start_below_actual_supervisor(self):
        self.assertTrue(all(t>=2 for _,_,t in self.router.candidates('researcher',1)))
        self.assertTrue(all(t==3 for _,_,t in self.router.candidates('researcher',3)))
    def test_platform_quota_blocks_all_free_models(self):
        self.router.record_error('openrouter','qwen/qwen3.8-27b:free',ProviderError(429,{'error':{'metadata':{}}},{'Retry-After':'120'}))
        self.assertGreater(self.router.blocked_until('openrouter','thinkingmachines/inkling:free'),time.time())
        self.assertEqual(self.router.blocked_until('google','gemini-3.8-flash'),0)
    def test_upstream_quota_blocks_only_one_model(self):
        self.router.record_error('openrouter','qwen/qwen3.8-27b:free',ProviderError(429,{'error':{'metadata':{'provider_code':429}}},{'Retry-After':'120'}))
        self.assertEqual(self.router.blocked_until('openrouter','thinkingmachines/inkling:free'),0)
    def test_cross_provider_tool_history(self):
        ai=AIMessage(content='',tool_calls=[{'id':'c1','name':'search','args':{'q':'Venice'}}],additional_kwargs={'routing_provider':'openrouter','routing_model':'qwen'})
        tool=ToolMessage(content='actual result',name='search',tool_call_id='c1')
        google=self.router.messages_for_google([ai,tool])
        self.assertFalse(google[0].tool_calls)
        self.assertIn('actual result',google[1].content)
        wire=self.router.messages_for_openrouter([ai,tool],'qwen')
        self.assertEqual(wire[0]['tool_calls'][0]['id'],wire[1]['tool_call_id'])
    async def test_catalog_refuses_paid_models(self):
        self.router.catalog=set();self.router.catalog_at=time.time()
        with self.assertRaises(ProviderError):
            await self.router.request('openrouter','paid-model',[],[])

class FreePreviewTests(unittest.TestCase):
    def test_accepts_zero_price_preview_without_free_suffix(self):
        self.assertTrue(free_tool_model({'id':'stealth/space-bunny-alpha','pricing':{'prompt':'0','completion':'0'},'supported_parameters':['tools']}))
    def test_rejects_paid_missing_pricing_and_non_tool_models(self):
        for model in [
            {'pricing':{'prompt':'0','completion':'0.0001'},'supported_parameters':['tools']},
            {'pricing':{'prompt':'0'},'supported_parameters':['tools']},
            {'pricing':{'prompt':'0','completion':'0','request':'0.01'},'supported_parameters':['tools']},
            {'pricing':{'prompt':'0','completion':'0'},'supported_parameters':[]},
        ]:self.assertFalse(free_tool_model(model))
    def test_skips_expired_preview(self):
        self.assertFalse(free_tool_model({'pricing':{'prompt':'0','completion':'0'},'supported_parameters':['tools'],'expiration_date':'2000-01-01'}))
