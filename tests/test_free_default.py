"""Free shared defaults preserve explicit personal model overrides."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from langchain_core.messages import AIMessage
from trvelle.orchestrator.model_accounts import AccountError
from trvelle.orchestrator.personal_models import PersonalModels,active_owner
from trvelle.orchestrator.model_router import ModelRouter


class FreeDefaultTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import uuid
        self.temp=tempfile.TemporaryDirectory()
        self.owner=str(uuid.uuid4())
        self.service=PersonalModels(Path(self.temp.name)/'accounts')
        self.router=ModelRouter(state_path=str(Path(self.temp.name)/'cooldowns.sqlite'))
        self.router.keys={'openrouter':'shared-test-key','google':'shared-google-key'}

    async def asyncTearDown(self):
        await self.service.close()
        self.router.cooldowns.db.close()
        for router in self.router.personal_routers.values():
            if router.cooldowns is not self.router.cooldowns:router.cooldowns.db.close()
        self.temp.cleanup()

    async def test_automatic_roles_use_free_router_without_selecting_connected_paid_models(self):
        self.router.request=AsyncMock(side_effect=[AIMessage(content='plan'),AIMessage(content='research')])
        self.service.invoke_auto=AsyncMock(side_effect=AssertionError('Default must not select a connected paid model'))
        token=active_owner.set(self.owner)
        try:
            with patch('trvelle.orchestrator.personal_models.get_accounts',return_value=self.service):
                for role in ('supervisor','researcher'):
                    result=await self.router.invoke(role,[],[])
                    self.assertEqual(result.additional_kwargs['routing_model'],'openrouter/free')
        finally:active_owner.reset(token)
        self.assertEqual([(call.args[0],call.args[1]) for call in self.router.request.call_args_list],[('openrouter','openrouter/free')]*2)
        self.service.invoke_auto.assert_not_called()

    async def test_paid_personal_selection_overrides_free_default_and_cannot_use_shared_key(self):
        model={'provider':'openrouter','id':'example/paid-planner','efforts':[],'free':False,'available':False}
        self.service.catalog=AsyncMock(return_value={'models':[model]})
        choice={'provider':'openrouter','model':model['id'],'effort':''}
        with self.assertRaises(AccountError):
            await self.service.save_roles(self.owner,{'supervisor':dict(choice)})
        self.service.set_key(self.owner,'openrouter','personal-test-openrouter-key')
        model['available']=True
        await self.service.save_roles(self.owner,{'supervisor':dict(choice)})
        async def request(owner,selection,messages,tools):
            self.assertEqual(self.service.credential(owner,'openrouter'),'personal-test-openrouter-key')
            self.assertFalse(selection['free'])
            return AIMessage(content='personal paid answer')
        self.service.request=AsyncMock(side_effect=request)
        self.router.request=AsyncMock(side_effect=AssertionError('Shared router must not handle a paid selection'))
        token=active_owner.set(self.owner)
        try:
            with patch('trvelle.orchestrator.personal_models.get_accounts',return_value=self.service):
                result=await self.router.invoke('supervisor',[],[])
        finally:active_owner.reset(token)
        self.assertEqual(result.additional_kwargs['routing_model'],model['id'])
        self.service.request.assert_awaited_once()
        self.router.request.assert_not_called()
