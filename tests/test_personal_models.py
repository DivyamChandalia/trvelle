import asyncio
import hashlib
import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
from langchain_core.messages import AIMessage, HumanMessage
from trvelle.orchestrator.model_accounts import ModelAccounts, AccountError
from trvelle.orchestrator.personal_models import PersonalModels, active_owner
from trvelle.orchestrator.model_router import (
    ModelRouter,
    ModelsUnavailableError,
    ProviderError,
    reset_time,
)


class PersonalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = PersonalModels(self.temp.name + "/accounts")
        self.owner = str(uuid.uuid4())
        self.other = str(uuid.uuid4())
        self.router = ModelRouter(state_path=self.temp.name + "/cooldowns.sqlite")

    async def asyncTearDown(self):
        await self.service.close()
        self.router.cooldowns.db.close()
        self.temp.cleanup()

    def test_keys_encrypted_private_and_owner_isolated(self):
        value = "private-test-key-12345"
        self.service.set_key(self.owner, "openai", value)
        self.assertEqual(self.service.load(self.owner)["keys"]["openai"], value)
        self.assertEqual(self.service.load(self.other)["keys"], {})
        self.assertNotIn(value, json.dumps(self.service.state(self.owner)))
        for path in Path(self.temp.name + "/accounts").rglob("*"):
            if path.is_file():
                self.assertNotIn(value.encode(), path.read_bytes())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.service.set_key(self.owner, "openai", "")
        self.assertFalse(self.service.state(self.owner)["keys"]["openai"])

    def test_path_and_invalid_provider_rejected(self):
        with self.assertRaises(ValueError):
            self.service.load("../other")
        with self.assertRaises(AccountError):
            self.service.set_key(self.owner, "bad", "private-test-key")
        with self.assertRaises(AccountError):
            self.service.set_key(self.owner, "openai", "key with whitespace")

    async def test_roles_validated_against_catalog_and_effort(self):
        self.service.set_key(self.owner,"openai","personal-role-test-key")
        self.service.catalog = AsyncMock(
            return_value={
                "models": [
                    {
                        "provider": "openai",
                        "id": "gpt-6.1-sol",
                        "efforts": ["medium"],
                        "available": True,
                    }
                ]
            }
        )
        choice = {"provider": "openai", "model": "gpt-6.1-sol", "effort": "medium"}
        await self.service.save_roles(
            self.owner, {"supervisor": choice, "researcher": None}
        )
        self.assertEqual(self.service.load(self.owner)["roles"]["supervisor"], choice)
        self.assertEqual(self.service.load(self.other)["roles"], {})
        with self.assertRaises(AccountError):
            await self.service.save_roles(
                self.owner, {"supervisor": dict(choice, effort="max")}
            )
        with self.assertRaises(AccountError):
            await self.service.save_roles(
                self.owner, {"supervisor": dict(choice, model="invented")}
            )
        await self.service.save_roles(
            self.owner, {"supervisor": None, "researcher": None}
        )
        self.assertEqual(self.service.load(self.owner)["roles"], {})

    def select(self, provider="openai", model="gpt-6.1-sol"):
        data = self.service.load(self.owner)
        data["keys"][provider] = "personal-test-key"
        data["roles"] = {
            "supervisor": {"provider": provider, "model": model, "effort": "medium"}
        }
        self.service.save(self.owner, data)

    async def test_selected_model_obeys_cooldown_and_does_not_fall_back(self):
        self.select()
        self.service.request = AsyncMock(
            side_effect=ProviderError(429, {}, {"Retry-After": "3600"})
        )
        with self.assertRaises(ModelsUnavailableError):
            await self.service.invoke_selected(
                self.router, self.owner, "supervisor", [], []
            )
        with self.assertRaises(ModelsUnavailableError):
            await self.service.invoke_selected(
                self.router, self.owner, "supervisor", [], []
            )
        self.assertEqual(self.service.request.await_count, 1)
        self.assertIsNone(
            await self.service.invoke_selected(
                self.router, self.other, "supervisor", [], []
            )
        )

    async def test_existing_shared_account_cooldown_cannot_be_bypassed(self):
        self.select("openrouter", "stealth/space-bunny-alpha")
        self.router.keys["openrouter"] = "personal-test-key"
        self.router.cooldowns.block(
            self.router.scope("openrouter"), time.time() + 3600, "account quota"
        )
        self.service.request = AsyncMock()
        with self.assertRaises(ModelsUnavailableError):
            await self.service.invoke_selected(
                self.router, self.owner, "supervisor", [], []
            )
        self.service.request.assert_not_awaited()

    async def test_routing_metadata_and_request_ownership(self):
        self.select()
        self.service.request = AsyncMock(return_value=AIMessage(content="hello"))
        token = active_owner.set(self.owner)
        try:
            with patch(
                "trvelle.orchestrator.personal_models.get_accounts",
                return_value=self.service,
            ):
                result = await self.router.invoke(
                    "supervisor", [], [HumanMessage(content="trip")]
                )
        finally:
            active_owner.reset(token)
        self.assertEqual(result.additional_kwargs["routing_model"], "gpt-6.1-sol")
        self.assertEqual(result.additional_kwargs["routing_effort"], "medium")
        self.assertEqual(self.service.request.call_args.args[0], self.owner)

    async def test_personal_key_is_used_with_default_roles_without_mutating_shared_router(
        self,
    ):
        self.service.set_key(self.owner, "google", "personal-google-key")
        original = self.router.keys["google"]
        token = active_owner.set(self.owner)
        try:
            with (
                patch(
                    "trvelle.orchestrator.personal_models.get_accounts",
                    return_value=self.service,
                ),
                patch.object(
                    ModelRouter,
                    "request",
                    AsyncMock(return_value=AIMessage(content="hello")),
                ),
            ):
                # Pin Google to avoid irrelevant OpenRouter configuration.
                self.router.role_models = {
                    "supervisor": ["google:gemini-3.5-flash-lite"]
                }
                await self.router.invoke("supervisor", [], [])
        finally:
            active_owner.reset(token)
        self.assertEqual(self.router.keys["google"], original)
        self.assertEqual(
            next(iter(self.router.personal_routers.values())).keys["google"],
            "personal-google-key",
        )

    async def test_vm_connection_exposes_tunnel_before_authorization(self):
        with patch.dict(os.environ, {"TRVELLE_MODEL_SSH_HOST": "ubuntu@144.24.127.147"}):
            response = await self.service.start_chatgpt(self.owner)
            redirect = urlsplit(parse_qs(urlsplit(response["url"]).query)["redirect_uri"][0])
            self.assertIn(f"127.0.0.1:{redirect.port}:127.0.0.1:{redirect.port}", response["tunnel_command"])
            self.assertTrue(response["tunnel_command"].endswith(" ubuntu@144.24.127.147"))
            self.assertEqual(await self.service.start_chatgpt(self.owner), response)
            state = self.service.state(self.owner)["accounts"]["chatgpt"]
            self.assertEqual(state["sign_in_url"], response["url"])
            self.assertIsNone(self.service.state(self.other)["accounts"]["chatgpt"]["sign_in_url"])

    async def test_vm_ssh_host_rejects_shell_arguments(self):
        with patch.dict(os.environ, {"TRVELLE_MODEL_SSH_HOST": "ubuntu@host; echo unsafe"}):
            with self.assertRaises(AccountError):
                await self.service.start_chatgpt(self.owner)
        self.assertNotIn((self.owner, "chatgpt"), self.service.pending)

    async def test_pkce_state_rejected_then_verified_account_saved(self):
        service = self.service
        previous = service.load(self.owner)
        previous["oauth"]["chatgpt"] = {"verified_models": {"gpt-6.1-sol": {"verified_at": 123}}}
        service.save(self.owner, previous)
        response = await service.start_chatgpt(self.owner)
        params = parse_qs(urlsplit(response["url"]).query)
        self.assertEqual(params["client_id"], ["dynamic_agent_client"])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        redirect = urlsplit(params["redirect_uri"][0])
        tokens = {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "id_token": "fake-id",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "openid offline_access resource.invoke chatgpt.tokens.use.direct",
        }
        transport = httpx.MockTransport(lambda req: httpx.Response(200, json=tokens))
        original = httpx.AsyncClient
        service.verify_identity = AsyncMock(
            return_value={"sub": "identity1", "email": "test@example.com"}
        )

        async def callback(query):
            reader, writer = await asyncio.open_connection("127.0.0.1", redirect.port)
            writer.write(
                f"GET /auth/callback?{query} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode()
            )
            await writer.drain()
            result = await reader.read()
            writer.close()
            await writer.wait_closed()
            return result

        result = await callback("state=wrong&code=fake&client_id=issued-client")
        self.assertIn(b"400", result)
        self.assertEqual(
            service.state(self.owner)["accounts"]["chatgpt"]["status"], "connecting"
        )
        with patch(
            "trvelle.orchestrator.model_accounts.httpx.AsyncClient",
            side_effect=lambda **kw: original(transport=transport, **kw),
        ):
            result = await callback(
                f"state={params['state'][0]}&code=fake&client_id=issued-client"
            )
        self.assertIn(b"200 OK", result)
        self.assertTrue(service.state(self.owner)["accounts"]["chatgpt"]["sharing"])
        self.assertEqual(await service.chatgpt_token(self.owner), "access-secret")
        self.assertEqual(service.load(self.owner)["oauth"]["chatgpt"]["verified_models"], previous["oauth"]["chatgpt"]["verified_models"])
        self.assertNotIn("access-secret", json.dumps(service.state(self.owner)))
        self.assertEqual(service.verify_identity.call_args.args[2], params["nonce"][0])

    async def test_identity_only_grant_does_not_authorize_inference(self):
        data = self.service.load(self.owner)
        data["oauth"]["chatgpt"] = {
            "access_token": "identity-token",
            "scopes": ["openid"],
            "expires_at": time.time() + 3600,
        }
        self.service.save(self.owner, data)
        with self.assertRaises(AccountError):
            await self.service.chatgpt_token(self.owner)

    async def test_pending_rotation_is_reused_without_repeating_refresh(self):
        data = self.service.load(self.owner)
        tokens = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
            "scope": "offline_access chatgpt.tokens.use.direct",
            "token_type": "Bearer",
            "id_token": "rotation-id",
        }
        data["oauth"]["chatgpt"] = {
            "client_id": "issued",
            "subject": "identity",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": 0,
            "scopes": ["chatgpt.tokens.use.direct"],
            "pending_refresh": tokens,
            "pending_refresh_received_at": time.time(),
        }
        self.service.save(self.owner, data)
        self.service.verify_identity = AsyncMock(return_value={"sub": "identity"})
        with patch(
            "trvelle.orchestrator.model_accounts.httpx.AsyncClient",
            side_effect=AssertionError("must not repeat refresh"),
        ):
            self.assertEqual(await self.service.chatgpt_token(self.owner), "new-access")
        self.assertNotIn(
            "pending_refresh", self.service.load(self.owner)["oauth"]["chatgpt"]
        )

    async def test_openai_stream_requires_completion_and_preserves_tool_calls(self):
        self.service.set_key(self.owner, "openai", "personal-test-key")
        choice = {"provider": "openai", "model": "gpt-6.1-sol", "effort": "medium"}
        seen = []

        def handler(req):
            seen.append(json.loads(req.content))
            event = {
                "type": "response.completed",
                "response": {
                    "id": "r1",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call1",
                            "name": "search",
                            "arguments": '{"q":"Singapore"}',
                        }
                    ],
                },
            }
            return httpx.Response(
                200, content=("data: " + json.dumps(event) + "\n\n").encode()
            )

        original = httpx.AsyncClient
        with patch(
            "trvelle.orchestrator.personal_models.httpx.AsyncClient",
            side_effect=lambda **kw: original(
                transport=httpx.MockTransport(handler), **kw
            ),
        ):
            result = await self.service.request(
                self.owner, choice, [HumanMessage(content="trip")], []
            )
        self.assertEqual(result.tool_calls[0]["args"], {"q": "Singapore"})
        self.assertFalse(seen[0]["store"])
        self.assertTrue(seen[0]["stream"])
        self.assertEqual(seen[0]["reasoning"], {"effort": "medium"})
        with patch(
            "trvelle.orchestrator.personal_models.httpx.AsyncClient",
            side_effect=lambda **kw: original(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(
                        200,
                        content=b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n',
                    )
                ),
                **kw,
            ),
        ):
            with self.assertRaises(ValueError):
                await self.service.request(self.owner, choice, [], [])

    async def test_incomplete_inference_does_not_grant_model_access_and_denial_removes_evidence(self):
        saved = self.service.load(self.owner)
        saved["oauth"]["chatgpt"] = {"access_token": "test-token", "client_id": "test-client", "scopes": ["chatgpt.tokens.use.direct"]}
        choice = {"provider": "chatgpt", "model": "gpt-6.1-sol", "effort": "medium"}
        saved["roles"] = {"supervisor": choice}
        self.service.save(self.owner, saved)
        self.service.chatgpt_token = AsyncMock(return_value="test-token")
        original = httpx.AsyncClient
        for stream in (
            b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n',
            b'data: {"type":"response.completed","response":{"output":[]}}\n\n',
            b'data: {"type":"response.completed","response":{"model":"gpt-5.6-sol","output":[{"type":"message","content":[{"type":"output_text","text":"Hello"}]}]}}\n\n',
        ):
            with patch("trvelle.orchestrator.personal_models.httpx.AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=stream)), **kw)):
                try:
                    await self.service.request(self.owner, choice, [], [])
                except ValueError:
                    pass
            self.assertNotIn("verified_models", self.service.load(self.owner)["oauth"]["chatgpt"])
        stream = b'data: {"type":"response.completed","response":{"output":[{"type":"message","content":[{"type":"output_text","text":"Hello"}]}]}}\n\n'
        with patch("trvelle.orchestrator.personal_models.httpx.AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=stream)), **kw)):
            await self.service.request(self.owner, choice, [], [])
        self.assertIn(choice["model"], self.service.load(self.owner)["oauth"]["chatgpt"]["verified_models"])
        self.service.request = AsyncMock(side_effect=ProviderError(403, {"error": {"code": "model_access_denied"}}))
        with self.assertRaises(ModelsUnavailableError):
            await self.service.invoke_selected(self.router, self.owner, "supervisor", [], [])
        self.assertNotIn(choice["model"], self.service.load(self.owner)["oauth"]["chatgpt"]["verified_models"])

    async def test_claude_has_no_filesystem_shell_or_external_mcp_tools(self):
        self.service.claude_state = AsyncMock(return_value={"status": "connected"})
        self.service.claude_run = AsyncMock(
            return_value=json.dumps(
                {"structured_output": {"content": "hello", "tool_calls": []}}
            )
        )
        await self.service.request(
            self.owner,
            {"provider": "claude", "model": "claude-opus-5-5", "effort": "medium"},
            [],
            [],
        )
        args = self.service.claude_run.call_args.args[1]
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", args)
        self.assertIn("--safe-mode", args)
        self.assertIn("--no-session-persistence", args)

    def test_claude_environment_never_inherits_host_api_keys_or_profile(self):
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "host-secret",
                "CLAUDE_CODE_OAUTH_TOKEN": "host-secret",
                "CLAUDE_CONFIG_DIR": "/host/profile",
            },
        ):
            env, directory = self.service.claude_env(self.owner)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(directory))
        self.assertNotEqual(directory, self.service.claude_env(self.other)[1])

    def test_provider_request_and_token_reset_headers(self):
        deadline, _ = reset_time(
            ProviderError(
                429,
                {},
                {
                    "x-ratelimit-reset-requests": "1m2s",
                    "x-ratelimit-reset-tokens": "2m",
                },
            ),
            1000,
        )
        self.assertEqual(deadline, 1120)
        deadline, _ = reset_time(
            ProviderError(
                429, {}, {"anthropic-ratelimit-requests-reset": "1970-01-01T00:20:00Z"}
            ),
            1000,
        )
        self.assertEqual(deadline, 1200)

    async def test_claude_reset_event_is_preserved(self):
        self.service.claude_state = AsyncMock(return_value={"status": "connected"})
        deadline = time.time() + 3600
        events = [
            {
                "type": "rate_limit_event",
                "rate_limit_info": {"status": "rejected", "resetsAt": deadline},
            },
            {"type": "result", "is_error": True, "result": "Subscription usage limit"},
        ]
        self.service.claude_run = AsyncMock(
            return_value="\n".join(json.dumps(e) for e in events)
        )
        with self.assertRaises(ProviderError) as raised:
            await self.service.request(
                self.owner,
                {"provider": "claude", "model": "claude-opus-5-5", "effort": "medium"},
                [],
                [],
            )
        self.assertEqual(reset_time(raised.exception, time.time())[0], deadline)


class ContextTests(unittest.TestCase):
    def test_streaming_request_keeps_owner_context_through_research_and_cleans_up(self):
        from trvelle.orchestrator.worker import perform
        from langchain_core.messages import AIMessage
        from unittest.mock import Mock, AsyncMock
        from trvelle.orchestrator import worker
        observed=[]
        owner=uuid.uuid4()
        class Agent:
            db_handler=Mock()
            load_chat_history=Mock()
            async def orchestrate_stream(self, message, config, resume=False):
                observed.append(active_owner.get())
                async def research():observed.append(active_owner.get())
                await asyncio.create_task(research())
                yield {'message':'test response'}
        with patch.object(worker,'store'),patch.object(worker,'get_accounts') as accounts:
            accounts.return_value.load.return_value={'keys':{},'oauth':{},'roles':{}}
            asyncio.run(perform(Agent(), {'user_id':owner,'chat_id':uuid.uuid4(),'run_id':uuid.uuid4(),'query':'test','resume':True,'choices':{},'search_limits':{'serpapi':6,'tavily':6}}))
        self.assertEqual(observed,[str(owner),str(owner)])
        self.assertIsNone(active_owner.get())

    def test_model_settings_authentication_and_owner_key_isolation(self):
        from fastapi.testclient import TestClient
        from trvelle.orchestrator import api_wrapper

        with tempfile.TemporaryDirectory() as root:
            service = PersonalModels(root)
            owner, other = str(uuid.uuid4()), str(uuid.uuid4())
            with (
                patch(
                    "trvelle.orchestrator.personal_models.get_accounts",
                    return_value=service,
                ),
                patch.dict(os.environ, {"BACKEND_API_TOKEN": "test-api-token"}),
            ):
                client = TestClient(api_wrapper.app)
                response = client.post(
                    "/model_key",
                    headers={"user-id": owner},
                    json={"provider": "openai", "key": "private-test-key"},
                )
                self.assertEqual(response.status_code, 401)
                response = client.post(
                    "/model_key",
                    headers={"x-backend-token": "test-api-token", "user-id": owner},
                    json={"provider": "openai", "key": "private-test-key"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("private-test-key", response.text)
                self.assertFalse(service.state(other)["keys"]["openai"])


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_recommendations_filter_specialized_models_and_use_current_ids(
        self,
    ):
        with tempfile.TemporaryDirectory() as root:
            service = PersonalModels(root)
            owner = str(uuid.uuid4())
            for provider in ("openai", "anthropic", "google", "openrouter"):
                service.set_key(owner, provider, "private-test-key-" + provider)
            service.claude_state = AsyncMock(return_value={"status": "disconnected"})

            def handler(req):
                host = req.url.host
                if host == "openrouter.ai":
                    return httpx.Response(
                        200,
                        json={
                            "data": [
                                {
                                    "id": identifier,
                                    "name": identifier,
                                    "supported_parameters": ["tools", "reasoning"],
                                    "pricing": {
                                        "prompt": "0.00001",
                                        "completion": "0.00005",
                                    },
                                }
                                for identifier in (
                                    "anthropic/claude-fable-5.1",
                                    "anthropic/claude-sonnet-5.5",
                                )
                            ]
                        },
                    )
                if host == "api.openai.com":
                    return httpx.Response(
                        200,
                        json={
                            "data": [
                                {"id": identifier}
                                for identifier in (
                                    "gpt-6.1-sol",
                                    "gpt-6-luna",
                                    "gpt-6-astra",
                                    "gpt-6-image",
                                )
                            ]
                        },
                    )
                if host == "api.anthropic.com":
                    return httpx.Response(
                        200,
                        json={
                            "data": [
                                {"id": identifier}
                                for identifier in (
                                    "claude-opus-5-5",
                                    "claude-sonnet-5-5",
                                    "claude-fable-5-1",
                                )
                            ]
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "models": [
                            {
                                "name": "models/" + identifier,
                                "supportedGenerationMethods": ["generateContent"],
                            }
                            for identifier in (
                                "gemini-3-pro-image",
                                "gemini-3.8-flash-lite-tts",
                                "gemini-3.1-pro-preview",
                                "gemini-3.5-flash-lite",
                            )
                        ]
                    },
                )

            original = httpx.AsyncClient
            with patch(
                "trvelle.orchestrator.personal_models.httpx.AsyncClient",
                side_effect=lambda **kw: original(
                    transport=httpx.MockTransport(handler), **kw
                ),
            ):
                result = await service.catalog(owner)
            self.assertFalse(
                any("image" in m["id"] or "tts" in m["id"] for m in result["models"])
            )
            pairs = {r["provider"]: r for r in result["recommendations"]}
            self.assertEqual(
                pairs["google"]["supervisor"]["model"], "gemini-3.1-pro-preview"
            )
            self.assertEqual(
                pairs["google"]["researcher"]["model"], "gemini-3.5-flash-lite"
            )
            self.assertEqual(
                pairs["anthropic"]["supervisor"]["model"], "claude-opus-5-5"
            )
            self.assertEqual(
                pairs["anthropic"]["researcher"]["model"], "claude-sonnet-5-5"
            )
            self.assertEqual(pairs["openai"]["supervisor"]["model"], "gpt-6.1-sol")
            self.assertEqual(pairs["openai"]["researcher"]["model"], "gpt-6-luna")
            self.assertEqual(pairs["openai"]["researcher"]["effort"], "medium")
            self.assertEqual(
                pairs["openrouter"]["supervisor"]["model"], "anthropic/claude-fable-5.1"
            )
            self.assertEqual(
                pairs["openrouter"]["researcher"]["model"],
                "anthropic/claude-sonnet-5.5",
            )
            await service.close()


class ClaudeToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_claude_arguments_are_decoded_for_travel_tools(self):
        from langchain_core.tools import tool

        @tool
        def search(q: str) -> str:
            """Search travel facts."""
            return q

        with tempfile.TemporaryDirectory() as root:
            service = PersonalModels(root)
            owner = str(uuid.uuid4())
            service.claude_state = AsyncMock(return_value={"status": "connected"})
            service.claude_run = AsyncMock(
                return_value=json.dumps(
                    {
                        "type": "result",
                        "structured_output": {
                            "content": "",
                            "tool_calls": [
                                {"name": "search", "args": '{"q":"Singapore"}'}
                            ],
                        },
                    }
                )
            )
            result = await service.request(
                owner,
                {"provider": "claude", "model": "claude-opus-5-5", "effort": "medium"},
                [],
                [search],
            )
            self.assertEqual(result.tool_calls[0]["args"], {"q": "Singapore"})
            await service.close()


class StreamRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_completed_output_preserves_streamed_text_and_tool_call(self):
        with tempfile.TemporaryDirectory() as root:
            service = PersonalModels(root)
            owner = str(uuid.uuid4())
            service.chatgpt_token = AsyncMock(return_value="test-oauth-token")
            selection = {
                "provider": "chatgpt",
                "model": "gpt-6-astra",
                "effort": "medium",
            }
            events = [
                {
                    "type": "response.output_item.added",
                    "item": {"id": "m1", "type": "message", "content": []},
                },
                {"type": "response.output_text.delta", "delta": "I will check that."},
                {
                    "type": "response.output_item.done",
                    "item": {
                        "id": "m1",
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "I will check that."}
                        ],
                    },
                },
                {
                    "type": "response.output_item.added",
                    "item": {
                        "id": "f1",
                        "type": "function_call",
                        "call_id": "call1",
                        "name": "search",
                        "arguments": "",
                    },
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "f1",
                    "delta": '{"q":"Singapore"}',
                },
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": "f1",
                    "arguments": '{"q":"Singapore"}',
                },
                {
                    "type": "response.completed",
                    "response": {"id": "response1", "output": []},
                },
            ]
            original = httpx.AsyncClient
            content = "".join(
                "data: " + json.dumps(event) + "\n\n" for event in events
            ).encode()
            with patch(
                "trvelle.orchestrator.personal_models.httpx.AsyncClient",
                side_effect=lambda **kw: original(
                    transport=httpx.MockTransport(
                        lambda r: httpx.Response(200, content=content)
                    ),
                    **kw,
                ),
            ):
                result = await service.request(
                    owner, selection, [HumanMessage(content="trip")], []
                )
            self.assertEqual(result.content, "I will check that.")
            self.assertEqual(result.tool_calls[0]["args"], {"q": "Singapore"})
            self.assertEqual(result.tool_calls[0]["id"], "call1")
            self.assertEqual(len(result.tool_calls), 1)
            await service.close()

    async def test_account_catalog_uses_actual_efforts_and_recommends_current_generation(
        self,
    ):
        with tempfile.TemporaryDirectory() as root:
            service = PersonalModels(root)
            owner = str(uuid.uuid4())
            saved = service.load(owner)
            saved["oauth"]["chatgpt"] = {
                "access_token": "test-token",
                "scopes": ["chatgpt.tokens.use.direct"],
            }
            service.save(owner, saved)
            service.chatgpt_token = AsyncMock(return_value="test-token")
            service.claude_state = AsyncMock(return_value={"status": "disconnected"})
            inference_requests = []

            def handler(req):
                if req.url.host == "api.openai.com":
                    if req.method == "POST":
                        inference_requests.append(json.loads(req.content)["model"])
                        event = {"type": "response.completed", "response": {"output": [{
                            "type": "function_call", "call_id": "probe", "name": "search",
                            "arguments": '{"q":"Rome"}',
                        }]}}
                        return httpx.Response(200, content=("data: " + json.dumps(event) + "\n\n").encode())
                    return httpx.Response(
                        200,
                        json={
                            "models": [
                                {
                                    "slug": "gpt-6-astra",
                                    "display_name": "GPT-6 Astra",
                                    "visibility": "list",
                                    "supported_reasoning_levels": [
                                        {"effort": "medium"},
                                        {"effort": "max"},
                                        {"effort": "ultra"},
                                    ],
                                },
                                {
                                    "slug": "gpt-5.6-sol",
                                    "visibility": "list",
                                    "supported_reasoning_levels": [
                                        {"effort": "medium"}
                                    ],
                                },
                                {
                                    "slug": "gpt-5.6-luna",
                                    "visibility": "list",
                                    "supported_reasoning_levels": [
                                        {"effort": "medium"},
                                        {"effort": "max"},
                                    ],
                                },
                                {"slug": "hidden-model", "visibility": "hide"},
                            ]
                        },
                    )
                if req.url.host == "openrouter.ai":
                    return httpx.Response(
                        200,
                        json={
                            "data": [
                                {
                                    "id": "openai/gpt-6.1-sol",
                                    "supported_parameters": ["tools"],
                                    "pricing": {"prompt": "0.1", "completion": "0.1"},
                                }
                            ]
                        },
                    )
                return httpx.Response(200, json={"models": [], "data": []})

            original = httpx.AsyncClient
            with patch(
                "trvelle.orchestrator.personal_models.httpx.AsyncClient",
                side_effect=lambda **kw: original(
                    transport=httpx.MockTransport(handler), **kw
                ),
            ):
                result = await service.catalog(owner)
            models = [m for m in result["models"] if m["provider"] == "chatgpt"]
            self.assertEqual(models[0]["efforts"], ["medium", "max"])
            self.assertFalse(any(m["id"] == "hidden-model" for m in models))
            pair = next(
                p for p in result["recommendations"] if p["provider"] == "chatgpt"
            )
            self.assertEqual(pair["supervisor"]["model"], "gpt-5.6-sol")
            self.assertEqual(pair["researcher"]["model"], "gpt-5.6-luna")
            self.assertIn("gpt-6.1-sol", result["provider_notes"]["chatgpt"])
            self.assertNotIn("need separate API access", result["provider_notes"]["chatgpt"])

            # The provider can serve releases absent from its model list.
            # Completed tool calls survive a restart as evidence for this owner;
            # loading/refreshing the picker itself makes no inference requests.
            with patch(
                "trvelle.orchestrator.personal_models.httpx.AsyncClient",
                side_effect=lambda **kw: original(transport=httpx.MockTransport(handler), **kw),
            ):
                for model in ("gpt-6.1-sol", "gpt-6-luna"):
                    answer = await service.request(owner, {"provider": "chatgpt", "model": model, "effort": "low"}, [HumanMessage(content="Call search for Rome")], [])
                    self.assertEqual(answer.tool_calls[0]["args"], {"q": "Rome"})
                reloaded = PersonalModels(root)
                reloaded.chatgpt_token = AsyncMock(return_value="test-token")
                reloaded.claude_state = AsyncMock(return_value={"status": "disconnected"})
                updated = await reloaded.catalog(owner, refresh=True)
                new_pair = next(p for p in updated["recommendations"] if p["provider"] == "chatgpt")
                self.assertEqual(new_pair["supervisor"]["model"], "gpt-6.1-sol")
                self.assertEqual(new_pair["researcher"]["model"], "gpt-6-luna")
                await reloaded.save_roles(owner, {role: new_pair[role] for role in ("supervisor", "researcher")})
                self.assertEqual(reloaded.load(owner)["roles"]["supervisor"]["model"], "gpt-6.1-sol")
                self.assertEqual(inference_requests, ["gpt-6.1-sol", "gpt-6-luna"])
                self.assertEqual(reloaded.load(uuid.uuid4())["oauth"], {})
                await reloaded.close()
            await service.close()


class ErrorFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_request_has_actionable_feedback_and_does_not_create_quota_cooldown(
        self,
    ):
        with tempfile.TemporaryDirectory() as root:
            service = PersonalModels(root)
            owner = str(uuid.uuid4())
            router = ModelRouter(state_path=root + "/limits.sqlite")
            service.set_key(owner, "openai", "private-test-key")
            data = service.load(owner)
            data["roles"] = {
                "supervisor": {
                    "provider": "openai",
                    "model": "gpt-6.1-sol",
                    "effort": "medium",
                }
            }
            service.save(owner, data)
            service.request = AsyncMock(
                side_effect=ProviderError(
                    400,
                    {
                        "error": {
                            "code": "unsupported_parameter",
                            "param": "reasoning.effort",
                            "message": "private-test-key must never appear",
                        }
                    },
                )
            )
            for _ in range(2):
                with self.assertRaises(ModelsUnavailableError) as raised:
                    await service.invoke_selected(router, owner, "supervisor", [], [])
                self.assertIn("unsupported_parameter", str(raised.exception))
                self.assertIn("reasoning.effort", str(raised.exception))
                self.assertNotIn("private-test-key", str(raised.exception))
            self.assertEqual(service.request.await_count, 2)
            self.assertIn(
                "unsupported_parameter",
                service.load(owner)["last_errors"]["supervisor"]["message"],
            )
            router.cooldowns.db.close()
            await service.close()

    async def test_one_forbidden_model_does_not_block_other_models(self):
        with tempfile.TemporaryDirectory() as root:
            service = PersonalModels(root)
            owner = str(uuid.uuid4())
            router = ModelRouter(state_path=root + "/limits.sqlite")
            service.set_key(owner, "openai", "private-test-key")
            data = service.load(owner)
            data["roles"] = {
                "supervisor": {
                    "provider": "openai",
                    "model": "restricted-model",
                    "effort": "",
                }
            }
            service.save(owner, data)
            service.request = AsyncMock(
                side_effect=[ProviderError(403, {}), AIMessage(content="success")]
            )
            with self.assertRaises(ModelsUnavailableError):
                await service.invoke_selected(router, owner, "supervisor", [], [])
            data = service.load(owner)
            data["roles"]["supervisor"]["model"] = "allowed-model"
            service.save(owner, data)
            result = await service.invoke_selected(router, owner, "supervisor", [], [])
            self.assertEqual(result.content, "success")
            self.assertEqual(service.request.await_count, 2)
            self.assertNotIn("supervisor", service.load(owner).get("last_errors", {}))
            router.cooldowns.db.close()
            await service.close()

class ResponseLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_completion_returns_without_waiting_for_socket_eof_and_preserves_reasoning(self):
        from langchain_core.messages import HumanMessage
        with tempfile.TemporaryDirectory() as root:
            service = PersonalModels(root)
            owner = str(uuid.uuid4())
            service.chatgpt_token = AsyncMock(return_value='test-token')
            closed = []
            payloads = []
            class PersistentStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield ('data: '+json.dumps({'type':'response.completed','response':{'id':'response1','output':[{'type':'reasoning','id':'r1','encrypted_content':'opaque-test-content','summary':[]},{'type':'message','content':[{'type':'output_text','text':'Finished'}]}]}})+'\n\n').encode()
                    raise AssertionError('Read attempted after response.completed')
                async def aclose(self):
                    closed.append(True)
            def handler(req):
                payloads.append(json.loads(req.content))
                return httpx.Response(200,stream=PersistentStream(),headers={'content-type':'text/event-stream'})
            original = httpx.AsyncClient
            with patch('trvelle.orchestrator.personal_models.httpx.AsyncClient',side_effect=lambda **kw:original(transport=httpx.MockTransport(handler),**kw)):
                selection={'provider':'chatgpt','model':'gpt-5.6-sol','effort':'medium'}
                result=await service.request(owner,selection,[HumanMessage(content='Finish planning')],[])
                self.assertEqual(result.content,'Finished')
                self.assertEqual(result.additional_kwargs['responses_reasoning'][0]['encrypted_content'],'opaque-test-content')
                await service.request(owner,selection,[HumanMessage(content='Finish planning'),result,HumanMessage(content='Continue')],[])
                await service.request(owner,{**selection, 'model': 'gpt-5.6-luna'},[result,HumanMessage(content='Continue with another model')],[])
                service.set_key(owner, 'openai', 'test-api-key')
                await service.request(owner,{**selection, 'provider': 'openai'},[result,HumanMessage(content='Continue with another provider')],[])
            self.assertEqual(len(closed),4)
            self.assertIn('reasoning.encrypted_content',payloads[0]['include'])
            self.assertTrue(any(item.get('encrypted_content')=='opaque-test-content' for item in payloads[1]['input']))
            self.assertFalse(any(item.get('encrypted_content') for item in payloads[2]['input']))
            self.assertFalse(any(item.get('encrypted_content') for item in payloads[3]['input']))
            await service.close()

class AutomaticRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_prefers_sol_and_skips_provider_cooldowns_without_requests(self):
        import hashlib, time
        with tempfile.TemporaryDirectory() as root:
            service = PersonalModels(root)
            owner = str(uuid.uuid4())
            router = ModelRouter(state_path=root + '/limits.sqlite')
            data = service.load(owner)
            data['oauth']['chatgpt'] = {'client_id': 'test-client'}
            service.save(owner, data)
            service.set_key(owner, 'openrouter', 'test-openrouter-key')
            service.catalog = AsyncMock(return_value={
                'models': [{'provider': 'chatgpt', 'id': 'gpt-5.6-sol', 'available': True}, {'provider': 'openrouter', 'id': 'test-free', 'available': True, 'free': True}],
                'recommendations': [{'provider': 'chatgpt', 'supervisor': {'provider': 'chatgpt', 'model': 'gpt-5.6-sol'}}, {'provider': 'openrouter', 'supervisor': {'provider': 'openrouter', 'model': 'test-free'}}],
            })
            service.request = AsyncMock(return_value=AIMessage(content='Planned'))
            first = await service.invoke_auto(router, owner, 'supervisor', [], [])
            self.assertEqual(first.additional_kwargs['routing_model'], 'gpt-5.6-sol')
            scope = 'chatgpt:' + hashlib.sha256(b'test-client').hexdigest()[:16] + ':gpt-5.6-sol'
            router.cooldowns.block(scope, time.time() + 3600, 'provider reset')
            second = await service.invoke_auto(router, owner, 'supervisor', [], [])
            self.assertEqual(second.additional_kwargs['routing_model'], 'test-free')
            self.assertEqual(service.request.await_count, 2)
            self.assertEqual(service.load(owner)['roles'], {})
            router.cooldowns.db.close()
            await service.close()


class ClaudeNativeLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.service=PersonalModels(self.temp.name+'/accounts')
        self.owner=str(uuid.uuid4());self.other=str(uuid.uuid4())

    async def asyncTearDown(self):
        with patch('os.killpg'):
            await self.service.close()
        self.temp.cleanup()

    async def test_native_url_code_verification_and_owner_isolation(self):
        native_url='https://claude.com/cai/oauth/authorize?client_id=native-cli-test&state=native-state'
        class Process:
            returncode=None
            pid=123456789
            def __init__(self):
                self.stdout=asyncio.StreamReader()
                self.stdout.feed_data(('If the browser did not open, visit: '+native_url+'\n').encode())
                self.written=[]
                parent=self
                class Input:
                    def write(self,data):parent.written.append(data)
                    async def drain(self):parent.stdout.feed_eof()
                self.stdin=Input()
            async def wait(self):self.returncode=0;return 0
        process=Process()
        with patch.object(self.service,'ensure_claude_bridge',new=AsyncMock()), patch.object(self.service,'claude_command',return_value=['native-claude']), patch('asyncio.create_subprocess_exec',new=AsyncMock(return_value=process)) as launch:
            result=await self.service.start_claude(self.owner)
            self.assertEqual(result['url'],native_url)
            self.assertTrue(result['code_required'])
            state=await self.service.claude_state(self.owner)
            self.assertEqual(state['sign_in_url'],native_url)
            self.assertEqual(state['status'],'connecting')
            with self.assertRaises(AccountError):await self.service.complete_claude(self.other,'valid-native-test-code')
            with self.assertRaises(AccountError):await self.service.complete_claude(self.owner,'code\ncommand')
            await self.service.complete_claude(self.owner,'valid-native-test-code')
            await self.service.pending[(self.owner,'claude')]['task']
            self.assertEqual(process.written,[b'valid-native-test-code\n'])
            self.assertEqual(self.service.pending[(self.owner,'claude')]['status'],'connected')
            self.assertNotIn('url',self.service.pending[(self.owner,'claude')])
            self.assertNotIn('code',self.service.load(self.owner))
            args,kwargs=launch.call_args
            self.assertEqual(args,('native-claude','auth','login','--claudeai'))
            self.assertTrue(Path(kwargs['env']['CLAUDE_CONFIG_DIR']).name.startswith('claude-'))
            self.assertFalse(Path(kwargs['env']['CLAUDE_CONFIG_DIR']).exists())

    async def test_native_disconnected_auth_status_is_not_an_access_error(self):
        _,directory=self.service.claude_env(self.owner)
        with patch.object(self.service,'claude_command',return_value=['native-claude']), patch.object(self.service,'claude_run',new=AsyncMock(return_value=json.dumps({'loggedIn':False,'authMethod':None,'configDirectory':str(directory)}))):
            state=await self.service.claude_state(self.owner)
            self.assertEqual(state['readiness'],'disconnected')
            self.assertTrue(state['can_connect'])
            self.assertFalse(state['can_plan'])

    async def test_missing_bridge_can_be_set_up_from_connect(self):
        with patch.object(self.service,'claude_command',side_effect=AccountError('Not installed')):
            state=await self.service.claude_state(self.owner)
            self.assertEqual(state['readiness'],'setup')
            self.assertTrue(state['can_connect'])
            self.assertFalse(state['can_plan'])
        self.assertTrue(str(self.service.claude_bridge_directory()).startswith(str(self.service.root)))
