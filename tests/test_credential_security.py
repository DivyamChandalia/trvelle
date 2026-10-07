import asyncio,base64,json,os,shutil,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from cryptography.fernet import Fernet
from trvelle.orchestrator.model_accounts import ModelAccounts,AccountError
from trvelle.orchestrator.credential_security import ClaudeVault,minimal_cli_env,sandbox_command
from trvelle.orchestrator.personal_models import PersonalModels,active_choices,active_owner

class CredentialSecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.accounts=PersonalModels(Path(self.temp.name)/'vault')
        self.owner,self.other=[str(uuid.uuid4()) for _ in range(2)]
    async def asyncTearDown(self):
        await self.accounts.close();self.temp.cleanup()

    def test_api_keys_and_oauth_are_bound_to_their_owner(self):
        secret='personal-secret-fixture'
        self.accounts.save(self.owner,{'keys':{'openai':secret},'oauth':{'chatgpt':{'access_token':'oauth-access-fixture'}},'roles':{}})
        source=self.accounts.directory(self.owner)/'accounts.enc'
        destination=self.accounts.directory(self.other)/'accounts.enc'
        shutil.copyfile(source,destination)
        with self.assertRaises(AccountError):self.accounts.load(self.other)
        self.assertEqual(self.accounts.load(self.owner)['keys']['openai'],secret)
        self.assertNotIn(secret,source.read_text())

    def test_external_key_is_required_when_configured_and_not_copied_into_vault(self):
        key=Path(self.temp.name)/'external.key';key.write_bytes(Fernet.generate_key());key.chmod(0o600)
        with patch.dict(os.environ,{'MODEL_ACCOUNTS_KEY_FILE':str(key)}):
            store=ModelAccounts(Path(self.temp.name)/'separated')
            store.set_key(self.owner,'google','google-secret-fixture')
            self.assertFalse((store.root/'master.key').exists())
        key.unlink()
        with patch.dict(os.environ,{'MODEL_ACCOUNTS_KEY_FILE':str(key)}),self.assertRaises(AccountError):ModelAccounts(Path(self.temp.name)/'broken')

    def test_legacy_ciphertext_is_migrated_without_changing_connection(self):
        expected={'keys':{'openai':'legacy-fixture-key'},'roles':{},'oauth':{'chatgpt':{'refresh_token':'legacy-refresh-fixture'}}}
        path=self.accounts.directory(self.owner)/'accounts.enc'
        path.write_bytes(self.accounts.cipher.encrypt(json.dumps(expected).encode()))
        self.assertEqual(self.accounts.load(self.owner),expected)
        envelope=json.loads(self.accounts.cipher.decrypt(path.read_bytes()))
        self.assertEqual(envelope['owner'],self.owner)
        self.assertEqual(envelope['purpose'],'accounts')

    async def test_claude_plaintext_is_sealed_and_only_exists_during_scoped_runtime(self):
        legacy=self.accounts.directory(self.owner)/'claude';legacy.mkdir()
        token=b'claude-token-fixture'
        (legacy/'.credentials.json').write_bytes(token)
        vault=ClaudeVault(self.accounts)
        async with vault.session(self.owner) as scope:
            profile=scope['directory']
            self.assertFalse(legacy.exists())
            self.assertEqual((profile/'.credentials.json').read_bytes(),token)
            self.assertNotIn(token,vault.path(self.owner).read_bytes())
            (profile/'.credentials.json').write_bytes(b'refreshed-claude-fixture')
        self.assertFalse(profile.exists())
        self.assertEqual(base64.b64decode(vault.load(self.owner)['files']['.credentials.json']),b'refreshed-claude-fixture')
        shutil.copyfile(vault.path(self.owner),vault.path(self.other))
        with self.assertRaises(AccountError):vault.load(self.other)
        await vault.purge(self.owner)
        self.assertEqual(vault.load(self.owner)['files'],{})

    async def test_profile_is_cleaned_and_refreshed_credentials_sealed_on_cancellation(self):
        entered=asyncio.Event();locations=[]
        async def job():
            async with ClaudeVault(self.accounts).session(self.owner) as scope:
                locations.append(scope['directory'])
                (scope['directory']/'.credentials.json').write_bytes(b'cancel-fixture')
                entered.set();await asyncio.Future()
        task=asyncio.create_task(job());await entered.wait();task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        self.assertFalse(locations[0].exists())
        self.assertTrue(ClaudeVault(self.accounts).load(self.owner)['files'])

    def test_child_environment_and_sandbox_do_not_expose_backend_credentials(self):
        with patch.dict(os.environ,{'OPENROUTER_API_KEY':'shared-fixture','GOOGLE_API_KEY':'google-fixture','DB_URI':'database-fixture','BACKEND_API_TOKEN':'backend-fixture','CREDENTIALS_DIRECTORY':'/run/credentials/backend','MODEL_ACCOUNTS_KEY_FILE':'/secret/key'}):
            env,_=self.accounts.claude_env(self.owner)
            for name in ('OPENROUTER_API_KEY','GOOGLE_API_KEY','DB_URI','BACKEND_API_TOKEN','CREDENTIALS_DIRECTORY','MODEL_ACCOUNTS_KEY_FILE'):self.assertNotIn(name,env)
        with patch.dict(os.environ,{'TRVELLE_CLAUDE_SANDBOX':'1'}),patch('shutil.which',return_value='/usr/bin/bwrap'):
            command,env,path=sandbox_command(['/bridge-package/bin/claude','auth','status'],Path(self.temp.name),minimal_cli_env())
        self.assertIn('--unshare-pid',command)
        self.assertIn('--cap-drop',command)
        self.assertNotIn(str(self.accounts.root),command)
        self.assertEqual(env['CLAUDE_CONFIG_DIR'],'/profile')
        self.assertEqual(str(path),'/profile')
        with patch.dict(os.environ,{'TRVELLE_CLAUDE_SANDBOX':'1'}),patch('shutil.which',return_value=None),self.assertRaises(AccountError):sandbox_command(['/cli'],Path(self.temp.name),{})

    def test_removed_personal_key_is_not_reused_from_a_running_snapshot(self):
        self.accounts.set_key(self.owner,'openai','saved-api-fixture-key')
        snapshot=active_choices.set(self.accounts.load(self.owner));owner=active_owner.set(self.owner)
        try:
            self.accounts.set_key(self.owner,'openai','')
            with patch.dict(os.environ,{'OPENAI_API_KEY':''}):self.assertFalse(self.accounts.credential(self.owner,'openai'))
        finally:active_choices.reset(snapshot);active_owner.reset(owner)

    def test_symlinked_user_storage_is_rejected(self):
        directory=self.accounts.root/self.other
        directory.symlink_to(self.accounts.directory(self.owner),target_is_directory=True)
        with self.assertRaises(AccountError):self.accounts.load(self.other)

    async def test_disconnect_disables_local_oauth_even_if_provider_revocation_fails(self):
        import httpx
        saved={'client_id':'fixture-client','subject':'fixture-subject','access_token':'access-fixture','refresh_token':'refresh-fixture','scopes':['chatgpt.tokens.use.direct'],'expires_at':9999999999}
        self.accounts.save(self.owner,{'keys':{'google':'other-provider-fixture'},'roles':{},'oauth':{'chatgpt':saved}})
        original=httpx.AsyncClient
        transport=httpx.MockTransport(lambda request:httpx.Response(503,request=request))
        with patch('httpx.AsyncClient',side_effect=lambda **kwargs:original(transport=transport,**kwargs)):
            with self.assertRaises(AccountError):await self.accounts.disconnect_chatgpt(self.owner)
        current=self.accounts.load(self.owner)
        self.assertNotIn('chatgpt',current['oauth'])
        self.assertIn('chatgpt',current['pending_revocations'])
        self.assertEqual(current['keys']['google'],'other-provider-fixture')
        with self.assertRaises(AccountError):await self.accounts.chatgpt_token(self.owner)
        expected={key:saved.get(key) for key in ('client_id','subject','refresh_token','access_token')}
        with self.assertRaises(AccountError):self.accounts.save_chatgpt(self.owner,saved,expected)
        self.assertNotIn('chatgpt',self.accounts.load(self.owner)['oauth'])

    def test_refresh_does_not_restore_an_api_key_removed_after_its_snapshot(self):
        saved={'client_id':'fixture-client','subject':'fixture-subject','access_token':'old-access','refresh_token':'old-refresh'}
        self.accounts.save(self.owner,{'keys':{'openai':'delete-me-fixture'},'roles':{},'oauth':{'chatgpt':saved}})
        expected=dict(saved)
        self.accounts.set_key(self.owner,'openai','')
        self.accounts.save_chatgpt(self.owner,{**saved,'access_token':'new-access'},expected)
        self.assertEqual(self.accounts.load(self.owner)['keys'],{})
