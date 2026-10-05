"""Guest linking must preserve history and private credentials across retries."""
import asyncio
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient
from trvelle.orchestrator.api_wrapper import app, db_handler
from trvelle.orchestrator.model_accounts import ModelAccounts, AccountError
from trvelle.database.models import User, ChatSession, Message, PlanningRun


class CredentialLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_keeps_existing_tokens_and_closes_pending_flow(self):
        with tempfile.TemporaryDirectory() as root:
            service = ModelAccounts(root)
            owner = uuid.uuid4()
            data = service.load(owner)
            data['oauth']['chatgpt'] = {'access_token': 'test-only', 'client_id': 'reuse-registration'}
            service.save(owner, data)
            task = asyncio.create_task(asyncio.sleep(60))
            service.pending[(str(owner), 'chatgpt')] = {'status': 'connecting', 'task': task}
            await service.cancel_sign_in(owner, 'chatgpt')
            self.assertTrue(task.cancelled())
            self.assertEqual(service.load(owner)['oauth'], data['oauth'])
            service.pending[(str(owner),'chatgpt')] = {'status':'disconnected','error':'Sign-in failed'}
            self.assertEqual(service.state(owner)['accounts']['chatgpt']['status'],'connected')
            service.pending.clear()
            self.assertNotIn((str(owner), 'chatgpt'), service.pending)

    def test_ready_expired_and_refreshable_states_do_not_expose_tokens(self):
        with tempfile.TemporaryDirectory() as root:
            service = ModelAccounts(root)
            owner = uuid.uuid4()
            data = service.load(owner)
            token = {'access_token': 'private-test-token', 'scopes': ['chatgpt.tokens.use.direct'], 'expires_at': time.time() - 1}
            data['oauth']['chatgpt'] = token
            service.save(owner, data)
            state = service.state(owner)['accounts']['chatgpt']
            self.assertEqual(state['readiness'], 'expired')
            self.assertFalse(state['can_plan'])
            token['refresh_token'] = 'private-refresh-token'
            service.save(owner, data)
            state = service.state(owner)['accounts']['chatgpt']
            self.assertEqual(state['readiness'], 'ready')
            self.assertTrue(state['can_plan'])
            self.assertNotIn('private-', str(state))


    async def test_invalid_refresh_is_not_retried_and_reset_headers_are_respected(self):
        for status in (401, 429):
            with tempfile.TemporaryDirectory() as root:
                service = ModelAccounts(root)
                owner = uuid.uuid4()
                data = service.load(owner)
                data['oauth']['chatgpt'] = {'access_token': 'old-access', 'refresh_token': 'test-refresh',
                    'client_id': 'test-client', 'expires_at': time.time()-1, 'scopes': ['chatgpt.tokens.use.direct']}
                service.save(owner, data)
                requests = []
                def respond(request):
                    requests.append(True)
                    return httpx.Response(status, json={'error': 'invalid_grant' if status==401 else 'rate_limit'}, headers={'retry-after':'3600'})
                original = httpx.AsyncClient
                with patch('trvelle.orchestrator.model_accounts.httpx.AsyncClient', side_effect=lambda **kw: original(transport=httpx.MockTransport(respond),**kw)):
                    for _ in range(2):
                        with self.assertRaises(AccountError):
                            await service.chatgpt_token(owner)
                self.assertEqual(len(requests),1)
                readiness = service.state(owner)['accounts']['chatgpt']['readiness']
                self.assertEqual(readiness, 'expired' if status==401 else 'refresh_pending')
                self.assertFalse(service.state(owner)['accounts']['chatgpt']['can_plan'])


class GuestMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.accounts = ModelAccounts(self.temp.name)
        self.patch = patch('trvelle.orchestrator.personal_models.get_accounts', return_value=self.accounts)
        self.patch.start()
        self.client = TestClient(app)
        self.guest, self.target, self.other, self.chat = [uuid.uuid4() for _ in range(4)]
        with db_handler.db_session() as db:
            db.add_all([User(user_id=id) for id in (self.guest, self.target, self.other)])
            db.flush()
            db.add(ChatSession(chat_id=self.chat, user_id=self.guest, session_name='Preserved plan'))
            db.flush()
            db.add(Message(message_id=uuid.uuid4(), chat_id=self.chat, user_id=self.guest, type='human', content='Guest request'))
            db.commit()
        self.accounts.set_key(self.guest, 'openai', 'guest-private-test')
        self.headers = {'x-backend-token': os.environ['BACKEND_API_TOKEN'], 'user-id': str(self.target)}

    def tearDown(self):
        with db_handler.db_session() as db:
            for identifier in (self.guest, self.target, self.other):
                db.delete(db.get(User, identifier))
            db.commit()
        self.client.close()
        self.patch.stop()
        self.temp.cleanup()

    def migrate(self):
        return self.client.post('/guest_migrate', headers=self.headers, json={'guest_id': str(self.guest)})

    def test_link_is_idempotent_preserves_destination_keys_and_chat_history(self):
        self.accounts.set_key(self.target, 'openai', 'destination-private-test')
        self.accounts.set_key(self.guest, 'google', 'guest-google-test')
        for _ in range(2):
            self.assertEqual(self.migrate().status_code, 200)
        with db_handler.db_session() as db:
            self.assertEqual(db.get(ChatSession, self.chat).user_id, self.target)
            self.assertEqual(db.query(Message).filter_by(chat_id=self.chat).one().user_id, self.target)
        self.assertEqual(self.accounts.load(self.target)['keys']['openai'], 'destination-private-test')
        self.assertEqual(self.accounts.load(self.target)['keys']['google'], 'guest-google-test')
        self.assertEqual(self.accounts.load(self.guest)['keys'], {})
        self.assertEqual(self.accounts.load(self.other)['keys'], {})
        self.assertEqual(len(db_handler.get_filtered_chat_history(self.target, self.chat)), 1)
        self.assertFalse(db_handler.get_filtered_chat_history(self.other, self.chat))

    def test_active_planning_and_auth_are_blocked_before_copying(self):
        with db_handler.db_session() as db:
            db.add(PlanningRun(chat_id=self.chat, user_id=self.guest, status='running', request={}))
            db.commit()
        self.assertEqual(self.migrate().status_code, 409)
        self.assertEqual(self.accounts.load(self.target)['keys'], {})
        with db_handler.db_session() as db:
            db.query(PlanningRun).filter_by(chat_id=self.chat).delete()
            db.commit()
        self.accounts.pending[(str(self.guest), 'claude')] = {'status': 'connecting'}
        self.assertEqual(self.migrate().status_code, 409)
        self.assertEqual(self.accounts.load(self.target)['keys'], {})

    def test_destination_planning_is_blocked_without_merging_credentials(self):
        with db_handler.db_session() as db:
            chat = ChatSession(chat_id=uuid.uuid4(), user_id=self.target)
            db.add(chat); db.flush()
            db.add(PlanningRun(chat_id=chat.chat_id, user_id=self.target, status='running', request={}))
            db.commit()
        response = self.migrate()
        self.assertEqual(response.status_code, 409)
        self.assertIn('Stop account planning', response.json()['detail'])
        self.assertFalse(self.accounts.load(self.target)['keys'])
        self.assertTrue(self.accounts.load(self.guest)['keys'])

    def test_copy_failure_keeps_history_and_credentials_retryable(self):
        with patch.object(self.accounts, 'copy_credentials', side_effect=AccountError('Finish or cancel your model sign-in before linking your guest account.')):
            self.assertEqual(self.migrate().status_code, 409)
        with db_handler.db_session() as db:
            self.assertEqual(db.get(ChatSession, self.chat).user_id, self.guest)
        self.assertTrue(self.accounts.load(self.guest)['keys'])
        self.assertEqual(self.migrate().status_code, 200)

    def test_credential_only_guest_and_claude_files_are_preserved_privately(self):
        owner = uuid.uuid4()
        self.accounts.set_key(owner, 'google', 'credential-only-test')
        source = self.accounts.directory(owner) / 'claude'
        source.mkdir(mode=0o700)
        (source / '.credentials.json').write_text('isolated-test-credentials')
        (source / 'symlink').symlink_to(Path(self.temp.name) / 'master.key')
        self.assertEqual(self.client.post('/guest_migrate', headers=self.headers, json={'guest_id': str(owner)}).status_code, 200)
        destination = self.accounts.directory(self.target) / 'claude'
        self.assertEqual((destination / '.credentials.json').read_text(), 'isolated-test-credentials')
        self.assertEqual((destination / '.credentials.json').stat().st_mode & 0o777, 0o600)
        self.assertFalse((destination / 'symlink').exists())
        self.assertFalse(source.exists())
        self.assertEqual(self.accounts.load(self.target)['keys']['google'], 'credential-only-test')

    def test_migration_requires_service_authorization(self):
        self.assertEqual(self.client.post('/guest_migrate', headers={'user-id': str(self.target)}, json={'guest_id': str(self.guest)}).status_code, 401)
