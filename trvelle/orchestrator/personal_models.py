"""User model choices, live catalogs and adapters for existing travel tools."""

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from contextvars import ContextVar
from pathlib import Path

import httpx
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from .model_accounts import ModelAccounts, AccountError
from .credential_security import ClaudeVault, minimal_cli_env, sandbox_command, audit

active_owner = ContextVar("model_account_owner", default=None)
active_choices = ContextVar("model_account_choices", default=None)
accounts = None

# Capability metadata for the releases whose account catalog currently lags
# inference access. Availability is established by a completed request, never
# by these public release names alone.
# https://developers.openai.com/api/docs/models/gpt-6.1-sol
# https://developers.openai.com/api/docs/models/gpt-6-luna
CHATGPT_RELEASE_MODELS = {
    "gpt-6.1-sol": {
        "name": "GPT-6.1 Sol",
        "efforts": ["low", "medium", "high", "xhigh", "max"],
    },
    "gpt-6-luna": {
        "name": "GPT-6 Luna",
        "efforts": ["none", "low", "medium", "high", "xhigh", "max"],
    },
}


def get_accounts():
    global accounts
    if accounts is None:
        accounts = PersonalModels()
    return accounts


class PersonalModels(ModelAccounts):
    def remember_chatgpt_model(self, owner, model):
        if model not in CHATGPT_RELEASE_MODELS:return
        def mutate(data):
            account=data['oauth'].get('chatgpt',{})
            if account.get('access_token') and 'chatgpt.tokens.use.direct' in account.get('scopes',[]):
                account.setdefault('verified_models',{}).setdefault(model,{'verified_at':time.time()})
        self.update(owner,mutate);self.catalogs.clear()

    def forget_chatgpt_model(self,owner,model):
        self.update(owner,lambda data:data['oauth'].get('chatgpt',{}).get('verified_models',{}).pop(model,None))
        self.catalogs.clear()

    def credential(self, owner, provider):
        data = self.load(owner)
        env = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "google": "GOOGLE_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        }
        audit(owner,"credential_requested",provider)
        return data["keys"].get(provider) or os.getenv(
            env.get(provider, ""), ""
        )

    def claude_env(self, owner, directory=None):
        env=minimal_cli_env()
        directory=Path(directory) if directory is not None else self.directory(owner)/'claude'
        env.update(HOME=str(directory),CLAUDE_CONFIG_DIR=str(directory),XDG_CONFIG_HOME=str(directory/'.config'),XDG_CACHE_HOME=str(directory/'.cache'),
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',CLAUDE_CODE_DISABLE_AUTO_MEMORY='1')
        return env,directory

    def claude_bridge_directory(self):
        package = Path(__file__).parents[2] / "model-bridge/package.json"
        version = json.loads(package.read_text())["dependencies"]["@anthropic-ai/claude-code"]
        return self.root / (".claude-bridge-" + version)

    def claude_command(self):
        for directory in (Path(__file__).parents[2] / "model-bridge", self.claude_bridge_directory()):
            cli = directory / "node_modules/.bin/claude"
            if cli.exists():
                return [str(cli)]
        raise AccountError("Claude Code needs setup. Choose Connect Claude to install the pinned bridge.")

    async def ensure_claude_bridge(self):
        try:
            self.claude_command()
            return
        except AccountError:
            pass
        if not hasattr(self, "_claude_install_lock"):
            self._claude_install_lock = asyncio.Lock()
        async with self._claude_install_lock:
            try:
                self.claude_command()
                return
            except AccountError:
                pass
            npm = shutil.which("npm") or ("/opt/node/bin/npm" if Path("/opt/node/bin/npm").exists() else None)
            if not npm:
                raise AccountError("Claude Code setup requires Node.js and npm on the server.")
            directory = self.claude_bridge_directory()
            directory.mkdir(mode=0o700, exist_ok=True)
            source = Path(__file__).parents[2] / "model-bridge"
            for name in ("package.json", "package-lock.json"):
                shutil.copyfile(source / name, directory / name)
            env = minimal_cli_env()
            env["HOME"] = str(directory)
            env["PATH"] = str(Path(npm).parent) + os.pathsep + env.get("PATH", "")
            command,env,_=sandbox_command([npm,"ci","--no-audit","--no-fund"],directory,env)
            process = await asyncio.create_subprocess_exec(*command,cwd=directory,
                env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
            try:
                await asyncio.wait_for(process.wait(), 120)
                if process.returncode:
                    raise AccountError("Claude Code setup failed. Retry connection or use a Claude API key.")
            finally:
                if process.returncode is None:
                    import signal
                    os.killpg(process.pid, signal.SIGTERM)
                    await process.wait()
            self.claude_command()

    async def claude_run(self, owner, args, input_data=None, timeout=15):
        async with ClaudeVault(self).session(owner) as scope:
            env,directory=self.claude_env(owner,scope['directory'])
            command,env,visible=sandbox_command([*self.claude_command(),*args],directory,env)
            process=await asyncio.create_subprocess_exec(*command,env=env,cwd=directory,stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,start_new_session=True)
            try:
                output,error=await asyncio.wait_for(process.communicate(input_data),timeout)
                if process.returncode and (("--output-format" not in args and args != ["auth","status"]) or not output.strip()):
                    raise AccountError('Claude request failed. Check your connection, model access and subscription limits.')
                if args==['auth','status']:
                    data=json.loads(output)
                    if Path(data.get('configDirectory','')).resolve()!=visible.resolve():
                        raise AccountError('Claude credential isolation could not be verified.')
                    scope['status']={key:data.get(key) for key in ('loggedIn','authMethod','email')}
                    data['configDirectory']=str(self.directory(owner)/'claude')
                    return json.dumps(data)
                return output.decode()
            finally:
                if process.returncode is None:
                    import signal
                    os.killpg(process.pid,signal.SIGTERM)
                    await process.wait()

    async def claude_state(self, owner):
        try:
            self.claude_command()
        except AccountError:
            return {"status": "disconnected", "readiness": "setup", "can_plan": False, "can_connect": True,
                    "message": "Connect Claude to set up the native Claude Code bridge. Your connection survives website deployments."}
        pending = self.pending.get((str(owner), "claude"), {})
        if pending.get("status") in ("connecting", "verifying"):
            return {"status": pending["status"], "readiness": pending["status"], "can_plan": False, "can_connect": True,
                    "sign_in_url": pending.get("url"), "code_required": True,
                    "message": "Sign in on Claude’s page, then paste its one-time code below. Claude Code completes the sign-in."}
        try:
            cached=ClaudeVault(self).load(owner)
            native_status=cached.get('status') if '.credentials.json' in cached.get('files',{}) else None
            data={**native_status,'configDirectory':str(self.directory(owner)/'claude')} if native_status else json.loads(await self.claude_run(owner, ["auth", "status"]))
            _, directory = self.claude_env(owner)
            if Path(data.get("configDirectory", "")).resolve() != directory.resolve():
                raise AccountError("Claude credential isolation could not be verified")
            return {
                "status": "connected"
                if data.get("loggedIn") and data.get("authMethod") == "claude.ai"
                else "disconnected",
                "email": data.get("email"),
                "readiness": "ready" if data.get("loggedIn") and data.get("authMethod") == "claude.ai" else "disconnected",
                "can_plan": bool(data.get("loggedIn") and data.get("authMethod") == "claude.ai"),
                "can_connect": True,
                "message": "Native connection through Claude Code. Your website login is separate.",
                "error": pending.get("error") if not data.get("loggedIn") else None,
            }
        except (AccountError, ValueError, OSError, asyncio.TimeoutError):
            return {"status": "disconnected", "readiness": "error", "can_plan": False, "can_connect": True,
                    "error": pending.get("error") or "Claude access could not be checked. Try connecting again."}

    async def start_claude(self, owner):
        await self.ensure_claude_bridge()
        slot = (str(owner), "claude")
        existing = self.pending.get(slot, {})
        if existing.get("status") in ("connecting", "verifying"):
            return {"url": existing.get("url"), "code_required": True}
        pending = {"status": "connecting", "ready": asyncio.Event()}
        self.pending[slot] = pending

        async def login():
            try:
                async with ClaudeVault(self).session(owner) as scope:
                    await native_login(scope)
            except (AccountError,OSError):
                pending.update(status='disconnected',error='Claude secure profile could not open. Please retry.')
                pending['ready'].set()

        async def native_login(scope):
            process = None
            try:
                env, directory = self.claude_env(owner,scope["directory"])
                # The unmodified CLI prints its own native sign-in URL when no
                # browser can open on the server. Tokens are handled by the CLI.
                env["BROWSER"] = "/bin/false"
                command,env,_=sandbox_command([*self.claude_command(),"auth","login","--claudeai"],directory,env)
                process = await asyncio.create_subprocess_exec(*command,
                    env=env, cwd=directory, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT, start_new_session=True)
                pending["process"] = process
                async with asyncio.timeout(600):
                    while line := await process.stdout.readline():
                        from urllib.parse import urlsplit
                        for candidate in re.findall(r"https://[^\s\x1b]+", line.decode(errors="replace")):
                            parsed = urlsplit(candidate)
                            if parsed.hostname in ("claude.com", "claude.ai") and parsed.path.endswith("/oauth/authorize"):
                                pending["url"] = candidate
                                pending["ready"].set()
                    await process.wait()
                    if process.returncode:
                        raise AccountError("Claude sign-in failed")
                scope["status"]=None
                audit(owner,"connected","claude")
                pending.update(status="connected", error=None)
                self.catalogs.clear()
            except (AccountError, OSError, asyncio.TimeoutError):
                pending.update(status="disconnected", error="Claude sign-in failed or timed out. Please reconnect.")
            finally:
                pending.pop("url", None)
                pending["ready"].set()
                if process and process.returncode is None:
                    import signal
                    os.killpg(process.pid, signal.SIGTERM)
                    await process.wait()

        pending["task"] = asyncio.create_task(login())
        try:
            await asyncio.wait_for(pending["ready"].wait(), 15)
        except asyncio.TimeoutError:
            await self.cancel_sign_in(owner, "claude")
            raise AccountError("Claude sign-in could not start. Please retry.") from None
        if not pending.get("url"):
            raise AccountError(pending.get("error") or "Claude sign-in could not start.")
        return {"url": pending["url"], "code_required": True,
                "message": "Open Claude sign-in, then paste the one-time code below. No SSH tunnel is needed."}

    async def complete_claude(self, owner, code):
        pending = self.pending.get((str(owner), "claude"), {})
        process = pending.get("process")
        if pending.get("status") != "connecting" or not process or process.returncode is not None:
            raise AccountError("Start Claude connection again before submitting a code.")
        if not code or len(code) > 8192 or any(character in code for character in "\r\n\x00"):
            raise AccountError("Enter the one-time code shown by Claude.")
        try:
            process.stdin.write((code.strip() + "\n").encode())
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            raise AccountError("Claude sign-in expired. Please reconnect.") from None
        pending["status"] = "verifying"
        return {"message": "Claude Code is verifying your sign-in."}

    async def disconnect_claude(self, owner):
        pending = self.pending.pop((str(owner), "claude"), {})
        if pending.get("task"):
            pending["task"].cancel()
            await asyncio.gather(pending["task"], return_exceptions=True)
        try:
            await self.claude_run(owner, ["auth", "logout"])
        finally:
            await ClaudeVault(self).purge(owner)
            self.catalogs.clear()

    async def settings(self, owner):
        result = self.state(owner)
        private = self.load(owner)
        result['roles'] = {role:choice for role,choice in result['roles'].items() if choice and (choice.get('provider') not in ('openai','anthropic','google','openrouter') or private['keys'].get(choice['provider']))}
        result["last_errors"] = private.get("last_errors", {})
        result["accounts"]["claude"] = await self.claude_state(owner)
        result["shared_keys"] = {
            p: bool(self.credential(owner, p)) and not result["keys"][p]
            for p in result["keys"]
        }
        return result

    async def catalog(self, owner, refresh=False):
        fingerprint = hashlib.sha256(
            json.dumps(self.load(owner), sort_keys=True).encode()
        ).hexdigest()
        cache_key = (str(owner), fingerprint)
        if (
            not refresh
            and cache_key in self.catalogs
            and time.time() - self.catalogs[cache_key]["fetched_at"] < 3600
        ):
            return self.catalogs[cache_key]
        from .model_router import free_tool_model

        models, errors = [], {}
        async with httpx.AsyncClient(timeout=15) as client:

            async def fetch(provider, url, headers=None):
                try:
                    response = await client.get(url, headers=headers)
                    response.raise_for_status()
                    return response.json()
                except Exception:
                    errors[provider] = "Model catalog unavailable; retry refresh."
                    return {}

            # OpenRouter catalog is public; price/capability metadata does not spend inference quota.
            public = await fetch("openrouter", "https://openrouter.ai/api/v1/models")
            for item in public.get("data", []):
                expiry = item.get("expiration_date")
                if expiry and expiry[:10] <= time.strftime("%Y-%m-%d", time.gmtime()):
                    continue
                if "tools" not in item.get("supported_parameters", []):
                    continue
                pricing = item.get("pricing", {})
                efforts = (
                    ["low", "medium", "high"]
                    if any(
                        "reasoning" in p for p in item.get("supported_parameters", [])
                    )
                    else []
                )
                models.append(
                    {
                        "provider": "openrouter",
                        "id": item["id"],
                        "name": item.get("name", item["id"]),
                        "efforts": efforts,
                        "pricing": pricing,
                        "free": free_tool_model(item),
                        "expiration_date": expiry,
                        "available": bool(self.load(owner)["keys"].get("openrouter")),
                        "created": item.get("created", 0),
                    }
                )
            for provider, url, headers in [
                (
                    "openai",
                    "https://api.openai.com/v1/models",
                    {"Authorization": f"Bearer {self.credential(owner, 'openai')}"},
                ),
                (
                    "anthropic",
                    "https://api.anthropic.com/v1/models",
                    {
                        "x-api-key": self.credential(owner, "anthropic"),
                        "anthropic-version": "2023-06-01",
                    },
                ),
                (
                    "google",
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    {"x-goog-api-key": self.credential(owner, "google")},
                ),
            ]:
                if not self.load(owner)['keys'].get(provider):
                    continue
                data = await fetch(provider, url, headers)
                for item in data.get("data", data.get("models", [])):
                    identifier = item.get("id") or item.get("name", "").removeprefix(
                        "models/"
                    )
                    if provider == "openai" and (
                        not identifier.startswith(("gpt-", "o3", "o4"))
                        or any(
                            t in identifier
                            for t in (
                                "audio",
                                "realtime",
                                "image",
                                "transcribe",
                                "search",
                                "tts",
                                "cyber",
                            )
                        )
                    ):
                        continue
                    if provider == "google" and (
                        "generateContent"
                        not in item.get("supportedGenerationMethods", [])
                        or not identifier.startswith("gemini")
                        or any(
                            word in identifier
                            for word in (
                                "image",
                                "tts",
                                "transcribe",
                                "audio",
                                "robotics",
                                "computer-use",
                                "omni",
                            )
                        )
                    ):
                        continue
                    efforts = (
                        ["low", "medium", "high", "xhigh", "max"]
                        if provider == "openai" and identifier.startswith("gpt-6")
                        else ["low", "medium", "high"]
                        if provider == "anthropic"
                        and any(
                            t in identifier for t in ("opus-5", "sonnet-5", "fable-5")
                        )
                        else []
                    )
                    models.append(
                        {
                            "provider": provider,
                            "id": identifier,
                            "name": item.get(
                                "display_name", item.get("displayName", identifier)
                            ),
                            "efforts": efforts,
                            "available": True,
                        }
                    )
            provider_notes = {}
            saved = self.load(owner)["oauth"].get("chatgpt", {})
            if saved.get("access_token") and "chatgpt.tokens.use.direct" in saved.get(
                "scopes", []
            ):
                try:
                    token = await self.chatgpt_token(owner)
                    data = await fetch(
                        "chatgpt",
                        "https://api.openai.com/v1/models",
                        {"Authorization": f"Bearer {token}"},
                    )
                    for item in data.get("models", []):
                        if item.get("visibility") == "list":
                            models.append(
                                {
                                    "provider": "chatgpt",
                                    "id": item["slug"],
                                    "name": item.get("display_name", item["slug"]),
                                    "efforts": [
                                        level["effort"]
                                        for level in item.get(
                                            "supported_reasoning_levels", []
                                        )
                                        if level.get("effort")
                                        in (
                                            "none",
                                            "low",
                                            "medium",
                                            "high",
                                            "xhigh",
                                            "max",
                                        )
                                    ],
                                    "default_effort": item.get(
                                        "default_reasoning_level", ""
                                    ),
                                    "available": True,
                                }
                            )
                    # A model-list rollout can lag behind usable inference.
                    # Only add releases this same OAuth account has completed;
                    # opening or refreshing the picker never spends inference.
                    offered = {
                        m["id"] for m in models if m["provider"] == "chatgpt"
                    }
                    verified = (
                        self.load(owner)["oauth"]
                        .get("chatgpt", {})
                        .get("verified_models", {})
                    )
                    for identifier, evidence in verified.items():
                        if (
                            identifier in offered
                            or identifier not in CHATGPT_RELEASE_MODELS
                        ):
                            continue
                        models.append(
                            {
                                "provider": "chatgpt",
                                "id": identifier,
                                **CHATGPT_RELEASE_MODELS[identifier],
                                "default_effort": "medium",
                                "available": True,
                                "access_note": "Verified through your ChatGPT connection; the provider catalog has not listed this release yet.",
                                "verified_at": evidence["verified_at"],
                            }
                        )
                except AccountError:
                    errors["chatgpt"] = (
                        "Reconnect ChatGPT to load your available models."
                    )
        if any(m["provider"] == "chatgpt" for m in models):
            offered = {m["id"] for m in models if m["provider"] == "chatgpt"}
            releases = {}
            for candidate in models:
                match = (
                    re.fullmatch(
                        r"openai/gpt-(\d+(?:\.\d+)?)-(sol|luna|astra)", candidate["id"]
                    )
                    if candidate["provider"] == "openrouter"
                    else None
                )
                if match:
                    version = tuple(int(part) for part in match[1].split("."))
                    if match[2] not in releases or version > releases[match[2]][0]:
                        releases[match[2]] = (version, candidate)
            released = [item[1] for item in releases.values()]
            missing = sorted({m["id"].split("/", 1)[1] for m in released} - offered)
            if missing:
                provider_notes["chatgpt"] = (
                    "The ChatGPT model catalog does not currently list "
                    + ", ".join(missing)
                    + ". Model-list rollout can lag behind inference access; absence from this list alone does not establish that your plan lacks access."
                )
        claude = await self.claude_state(owner)
        if claude["status"] == "connected":
            # Subscription CLI uses documented native IDs; entitlement is checked at inference.
            for item in models.copy():
                if item["provider"] == "openrouter" and re.fullmatch(
                    r"anthropic/claude-(opus|sonnet|fable)-[\d.]+", item["id"]
                ):
                    identifier = item["id"].split("/")[1].replace(".", "-")
                    models.append(
                        {
                            "provider": "claude",
                            "id": identifier,
                            "name": item["name"],
                            "efforts": ["low", "medium", "high"],
                            "available": True,
                            "access_note": "Model access depends on your Claude subscription.",
                        }
                    )
        # Recommendations are explicit heuristics, filtered against live provider catalogs.
        recommendations = []
        for provider in (
            "openai",
            "chatgpt",
            "anthropic",
            "claude",
            "openrouter",
            "google",
        ):
            items = [m for m in models if m["provider"] == provider and m["available"]]

            def latest(family):
                matches = [
                    m
                    for m in items
                    if family in m["id"]
                    and not any(x in m["id"] for x in ("audio", "realtime"))
                ]
                return max(
                    matches,
                    key=lambda m: tuple(int(x) for x in re.findall(r"\d+", m["id"])),
                    default=None,
                )

            orch = (
                latest("opus")
                if provider in ("claude", "anthropic")
                else latest("sol")
                if provider in ("chatgpt", "openai")
                else latest("pro")
                if provider == "google"
                else latest("fable")
                or latest("opus")
                or latest("sol")
                or latest("qwen")
                or latest("nemotron")
            )
            research = (
                (latest("sonnet") or latest("inkling-small") or latest("nemotron"))
                if provider in ("claude", "anthropic", "openrouter")
                else latest("luna")
                if provider in ("chatgpt", "openai")
                else latest("flash-lite") or latest("flash")
            )
            if orch and research and orch["id"] != research["id"]:
                recommendations.append(
                    {
                        "provider": provider,
                        "supervisor": {
                            "provider": provider,
                            "model": orch["id"],
                            "effort": "medium" if "medium" in orch["efforts"] else "",
                        },
                        "researcher": {
                            "provider": provider,
                            "model": research["id"],
                            "effort": "medium"
                            if "medium" in research["efforts"]
                            else "",
                        },
                        "reason": "Stronger planning model with a faster, cheaper researcher. Recommendations use provider model families, not a universal intelligence ranking.",
                    }
                )
        result = {
            "models": models,
            "recommendations": recommendations,
            "errors": errors,
            "provider_notes": provider_notes,
            "fetched_at": time.time(),
        }
        self.catalogs[cache_key] = result
        return result

    async def save_roles(self, owner, roles):
        catalog = await self.catalog(owner)
        for role, selection in roles.items():
            if role not in ("supervisor", "researcher"):
                raise AccountError("Invalid model role")
            if not selection:
                continue
            if selection['provider'] in ('openai','anthropic','google','openrouter') and not self.load(owner)['keys'].get(selection['provider']):
                raise AccountError('Add your own API key to choose models. App-provided access uses automatic routing only.')
            model = next(
                (
                    m
                    for m in catalog["models"]
                    if m["provider"] == selection["provider"]
                    and m["id"] == selection["model"]
                ),
                None,
            )
            if not model or not model["available"]:
                raise AccountError(
                    "Connect this provider and choose a model from its current catalog"
                )
            if selection.get("effort") and selection["effort"] not in model["efforts"]:
                raise AccountError(
                    "This reasoning effort is not supported by the selected model"
                )
            selection["free"] = model.get("free", False)
            selection["expiration_date"] = model.get("expiration_date")
        self.update(owner,lambda data:data.update(roles={role:choice for role,choice in roles.items() if choice}))

    async def invoke_selected(self, router, owner, role, messages, tools):
        from .model_router import ProviderError, ModelsUnavailableError

        data=self.load(owner)
        if active_choices.get() is not None:data={**data,"roles":active_choices.get().get("roles",{})}
        selection = data["roles"].get(role)
        if not selection:
            return None
        provider, model = selection["provider"], selection["model"]
        if provider in ('openai','anthropic','google','openrouter') and not data['keys'].get(provider):
            return None  # Legacy shared-key choices become automatic without a paid key.
        expiry = selection.get("expiration_date")
        if expiry and expiry[:10] <= time.strftime("%Y-%m-%d", time.gmtime()):
            raise ModelsUnavailableError(
                "The selected model has expired. Refresh the catalog and choose another model."
            )
        # OAuth account identity and API credential fingerprint isolate every cooldown.
        identity = (
            data["oauth"].get("chatgpt", {}).get("client_id", "")
            if provider == "chatgpt"
            else str(owner)
            if provider == "claude"
            else self.credential(owner, provider)
        )
        if not identity:
            raise ModelsUnavailableError(
                "The selected provider is disconnected. Reconnect it or choose another model."
            )
        scope = f"{provider}:{hashlib.sha256(identity.encode()).hexdigest()[:16]}"
        scopes = [scope + ":*", scope + ":" + model]
        if provider == "openrouter" and (
            selection.get("free") or model.endswith(":free")
        ):
            scopes.append(scope + ":free")
        until = max(router.cooldowns.until(s) for s in scopes)
        if until > time.time():
            from datetime import datetime, timezone

            raise ModelsUnavailableError(
                "Selected model is awaiting reset: "
                + datetime.fromtimestamp(until, timezone.utc).isoformat()
            )
        async with router.locks.setdefault((scope, model), asyncio.Lock()):
            if max(router.cooldowns.until(s) for s in scopes) > time.time():
                raise ModelsUnavailableError(
                    "Selected model is awaiting its provider reset"
                )
            try:
                # Long reasoning streams may legitimately exceed three minutes.
                # HTTP read timeout still rejects an idle/broken connection.
                request_limit = 900 if provider in ("chatgpt", "openai") and selection.get("effort") in ("max", "ultra") else 360 if provider in ("chatgpt", "openai") else 180
                response = await asyncio.wait_for(
                    self.request(owner, selection, messages, tools), request_limit
                )
                if not response.content and not response.tool_calls:
                    raise ValueError("Empty model response")
                self.update(owner,lambda data:data.get('last_errors',{}).pop(role,None))
                response.additional_kwargs.update(
                    routing_provider=provider,
                    routing_model=model,
                    routing_tier=0 if role == "supervisor" else 1,
                    routing_degraded=False,
                    routing_effort=selection.get("effort"),
                )
                return response
            except asyncio.CancelledError:
                raise
            except Exception as error:
                from .model_router import reset_time

                until, detail = reset_time(error, time.time())
                status = getattr(error, "status", None) or getattr(error, "code", None)
                if not isinstance(status, int):
                    status = getattr(
                        getattr(error, "response", None), "status_code", None
                    )
                if provider == "chatgpt" and status in (403, 404):
                    self.forget_chatgpt_model(owner, model)
                # Provider reset hints win; brief transport failures and credentials have separate scopes.
                until = until or time.time() + (
                    86400 if status in (401, 403, 402, 404) else 60
                )
                block_scope = (
                    scope + ":*" if status in (401, 402) else scope + ":" + model
                )
                if provider == "openrouter" and status == 429 and selection.get("free"):
                    metadata = (
                        getattr(error, "payload", {})
                        .get("error", {})
                        .get("metadata", {})
                    )
                    if not any(
                        k in metadata for k in ("provider_code", "provider_name")
                    ):
                        block_scope = scope + ":free"
                if status not in (400, 422):
                    router.cooldowns.block(
                        block_scope,
                        until,
                        "authentication required"
                        if status == 401
                        else "personal provider unavailable",
                    )
                label = {
                    "chatgpt": "ChatGPT",
                    "claude": "Claude",
                    "openai": "OpenAI",
                    "anthropic": "Claude API",
                    "google": "Gemini",
                    "openrouter": "OpenRouter",
                }[provider]
                from datetime import datetime, timezone

                retry = datetime.fromtimestamp(until, timezone.utc).isoformat()
                if isinstance(error, AccountError):
                    message = str(error)
                elif status in (401, 403):
                    message = f"{label} denied access to {model}. Reconnect the account or check this model is included in your plan."
                elif status == 402:
                    message = f"{label} has no available credits. Check provider billing or choose another connected provider."
                elif status == 429:
                    message = f"{label} usage limit reached for {model}. Retry after {retry}, or choose another model."
                elif status in (400, 404, 422):
                    payload = getattr(error, "payload", {})
                    issue = (
                        payload.get("error", {}) if isinstance(payload, dict) else {}
                    )
                    code = re.sub(
                        r"[^a-zA-Z0-9_.-]",
                        "",
                        str(
                            issue.get("code") or issue.get("type") or "invalid_request"
                        ),
                    )[:80]
                    param = re.sub(
                        r"[^a-zA-Z0-9_.\[\]-]", "", str(issue.get("param") or "")
                    )[:80]
                    message = (
                        f"{label} rejected the request for {model} ({status}: {code}"
                        + (f", {param}" if param else "")
                        + "). Check model access or change reasoning effort."
                    )
                elif isinstance(error, (asyncio.TimeoutError, httpx.TimeoutException)):
                    message = f"{label} did not finish within the request time limit. Your research is saved. Resume after {retry}, or use a lower reasoning effort. This is an app retry delay, not a provider quota reset."
                elif isinstance(error, ValueError):
                    message = f"{label} returned an incomplete or invalid response for {model}. Retry after {retry}."
                else:
                    message = f"{label} is temporarily unavailable for {model}. Retry after {retry}."
                import logging

                logging.getLogger(__name__).warning(
                    "Model request failed: provider=%s model=%s status=%s error_type=%s retry_at=%s",
                    provider,
                    model,
                    status,
                    type(error).__name__,
                    retry,
                )
                stored = self.load(owner)
                stored.setdefault("last_errors", {})[role] = {
                    "model": model,
                    "provider": provider,
                    "message": message,
                    "retry_at": retry,
                }
                self.update(owner,lambda data:data.setdefault("last_errors",{}).update({role:stored["last_errors"][role]}))
                raise ModelsUnavailableError(message) from None

    async def invoke_auto(self, router, owner, role, messages, tools):
        """Curated live catalog, connected subscriptions first; no automatic paid API choice."""
        from .model_router import ModelsUnavailableError
        data=self.load(owner)
        if active_choices.get() is not None:data={**data,"roles":active_choices.get().get("roles",{})}
        catalog = await self.catalog(owner)
        recommendations = catalog['recommendations']
        priority = ('chatgpt', 'claude', 'openrouter')
        available = {(m['provider'], m['id']): m for m in catalog['models'] if m['available']}
        for provider in priority:
            pair = next((item for item in recommendations if item['provider'] == provider), None)
            if pair is None:
                continue
            selected = pair[role]
            model = available.get((provider, selected['model']))
            if model is None or (provider == 'openrouter' and not model.get('free')):
                continue
            selected = {**selected, 'free': model.get('free', False), 'expiration_date': model.get('expiration_date')}
            token = active_choices.set({'roles': {**data['roles'], role: selected}})
            try:
                response = await self.invoke_selected(router, owner, role, messages, tools)
                if response:
                    response.additional_kwargs['routing_mode'] = 'auto'
                    return response
            except ModelsUnavailableError:
                # invoke_selected already persisted the provider's precise cooldown.
                continue
            finally:
                active_choices.reset(token)
        return None

    @staticmethod
    def functions(tools):
        return [convert_to_openai_tool(t)["function"] for t in tools]

    @staticmethod
    def transcript(messages):
        return "\n".join(
            json.dumps(
                {
                    "role": m.type,
                    "content": m.content,
                    **(
                        {"tool_calls": m.tool_calls}
                        if isinstance(m, AIMessage) and m.tool_calls
                        else {}
                    ),
                    **(
                        {"tool_call_id": m.tool_call_id}
                        if isinstance(m, ToolMessage)
                        else {}
                    ),
                },
                ensure_ascii=False,
            )
            for m in messages
        )

    async def request(self, owner, selection, messages, tools):
        from .model_router import ModelRouter, ProviderError

        provider, model, effort = (
            selection["provider"],
            selection["model"],
            selection.get("effort"),
        )
        if provider == "claude":
            if (await self.claude_state(owner))["status"] != "connected":
                raise AccountError("Connect your Claude subscription first")
            functions = self.functions(tools)
            schema = {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "tool_calls": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "enum": [f["name"] for f in functions] or ["none"],
                                },
                                "args": {
                                    "type": "string",
                                    "description": "A JSON encoded object of tool arguments matching its schema",
                                },
                            },
                            "required": ["name", "args"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["content", "tool_calls"],
                "additionalProperties": False,
            }
            instructions = (
                "You are a Trvelle travel agent. Respond with the specified JSON. Request travel tools through tool_calls, encoding args as a JSON string; Trvelle executes them. Never claim a tool result until it is provided. Available tool schemas: "
                + json.dumps(functions)
            )
            args = [
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--model",
                model,
                "--tools",
                "",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--setting-sources",
                "",
                "--safe-mode",
                "--no-session-persistence",
                "--system-prompt",
                instructions,
                "--json-schema",
                json.dumps(schema),
            ]
            if effort:
                args += ["--effort", effort]
            raw = await self.claude_run(
                owner, args, self.transcript(messages).encode(), timeout=175
            )
            events = [json.loads(line) for line in raw.splitlines() if line.strip()]
            body = next(
                (
                    event
                    for event in reversed(events)
                    if event.get("type") == "result" or "structured_output" in event
                ),
                {},
            )
            rejected = [
                event.get("rate_limit_info", {})
                for event in events
                if event.get("type") == "rate_limit_event"
                and event.get("rate_limit_info", {}).get("status") == "rejected"
            ]
            if body.get("is_error") or rejected:
                resets = [
                    info.get("resetsAt") or info.get("resets_at") for info in rejected
                ]
                resets = [float(value) for value in resets if value is not None]
                headers = {"x-ratelimit-reset": str(max(resets))} if resets else {}
                raise ProviderError(
                    429,
                    {
                        "error": {
                            "message": body.get("result", "Claude subscription limit")
                        }
                    },
                    headers,
                )
            output = body.get("structured_output")
            if not isinstance(output, dict):
                raise ValueError("Claude did not produce a structured travel response")
            return AIMessage(
                content=output["content"],
                tool_calls=[
                    {
                        "id": "claude-" + uuid.uuid4().hex,
                        "name": c["name"],
                        "args": json.loads(c["args"]),
                    }
                    for c in output["tool_calls"]
                ],
            )
        if provider == "google":
            from langchain_google_genai import ChatGoogleGenerativeAI

            return (
                await ChatGoogleGenerativeAI(
                    model=model,
                    google_api_key=self.credential(owner, provider),
                    timeout=90,
                    max_retries=0,
                )
                .bind_tools(tools)
                .ainvoke(ModelRouter.messages_for_google(messages))
            )
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15, read=180, write=30, pool=15)) as client:
            if provider == "anthropic":
                system = "\n".join(
                    str(m.content) for m in messages if m.type == "system"
                )
                wire = []
                for m in messages:
                    if m.type == "system":
                        continue
                    if isinstance(m, ToolMessage):
                        item = {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": m.tool_call_id,
                                    "content": str(m.content),
                                }
                            ],
                        }
                    elif isinstance(m, AIMessage):
                        blocks = (
                            [{"type": "text", "text": m.text}] if m.text else []
                        ) + [
                            {
                                "type": "tool_use",
                                "id": c["id"],
                                "name": c["name"],
                                "input": c["args"],
                            }
                            for c in m.tool_calls
                        ]
                        item = {"role": "assistant", "content": blocks}
                    else:
                        item = {
                            "role": "user",
                            "content": [{"type": "text", "text": m.text or ""}],
                        }
                    if wire and wire[-1]["role"] == item["role"]:
                        wire[-1]["content"] += item["content"]
                    else:
                        wire.append(item)
                payload = {
                    "model": model,
                    "system": system,
                    "messages": wire,
                    "max_tokens": 8192,
                }
                if tools:
                    payload["tools"] = [
                        {
                            "name": f["name"],
                            "description": f.get("description", ""),
                            "input_schema": f["parameters"],
                        }
                        for f in self.functions(tools)
                    ]
                if effort:
                    payload.update(
                        thinking={"type": "adaptive"}, output_config={"effort": effort}
                    )
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self.credential(owner, provider),
                        "anthropic-version": "2023-06-01",
                    },
                    json=payload,
                )
                body = response.json()
                if response.is_error:
                    raise ProviderError(response.status_code, body, response.headers)
                return AIMessage(
                    content="\n".join(
                        b["text"] for b in body["content"] if b["type"] == "text"
                    ),
                    tool_calls=[
                        {"id": b["id"], "name": b["name"], "args": b["input"]}
                        for b in body["content"]
                        if b["type"] == "tool_use"
                    ],
                )
            if provider == "openrouter":
                payload = {
                    "model": model,
                    "messages": ModelRouter.messages_for_openrouter(messages, model),
                    "max_tokens": 8192,
                }
                if effort:
                    payload["reasoning"] = {"effort": effort}
                if tools:
                    payload.update(
                        tools=[
                            {"type": "function", "function": f}
                            for f in self.functions(tools)
                        ],
                        provider={"require_parameters": True},
                    )
                response = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.credential(owner, provider)}"
                    },
                    json=payload,
                )
                body = response.json()
                if response.is_error or body.get("error"):
                    raise ProviderError(
                        response.status_code
                        if response.is_error
                        else body["error"].get("code", 503),
                        body,
                        response.headers,
                    )
                m = body["choices"][0]["message"]
                return AIMessage(
                    content=m.get("content") or "",
                    tool_calls=[
                        {
                            "id": c["id"],
                            "name": c["function"]["name"],
                            "args": json.loads(c["function"]["arguments"]),
                        }
                        for c in m.get("tool_calls", [])
                    ],
                    additional_kwargs={
                        "reasoning_details": m.get("reasoning_details", [])
                    },
                )
            if provider not in ("openai", "chatgpt"):
                raise AccountError("Unknown model provider")
            token = (
                await self.chatgpt_token(owner)
                if provider == "chatgpt"
                else self.credential(owner, provider)
            )
            wire = []
            for m in messages:
                if isinstance(m, ToolMessage):
                    wire.append(
                        {
                            "type": "function_call_output",
                            "call_id": m.tool_call_id,
                            "output": str(m.content),
                        }
                    )
                elif isinstance(m, AIMessage):
                    if m.additional_kwargs.get("reasoning_scope") == {"provider": provider, "model": model}:
                        wire.extend(m.additional_kwargs.get("responses_reasoning", []))
                    if m.text:
                        wire.append({"role": "assistant", "content": m.text})
                    wire += [
                        {
                            "type": "function_call",
                            "call_id": c["id"],
                            "name": c["name"],
                            "arguments": json.dumps(c["args"]),
                            **(
                                {"namespace": "trvelle"}
                                if provider == "chatgpt"
                                else {}
                            ),
                        }
                        for c in m.tool_calls
                    ]
                else:
                    wire.append(
                        {
                            "role": "developer" if m.type == "system" else "user",
                            "content": m.text or "",
                        }
                    )
            payload = {"model": model, "input": wire, "store": False, "stream": True, "include": ["reasoning.encrypted_content"]}
            if effort:
                payload["reasoning"] = {"effort": effort}
            if tools:
                functions = [
                    dict(f, type="function", strict=False)
                    for f in self.functions(tools)
                ]
                payload["tools"] = (
                    [
                        {
                            "type": "namespace",
                            "name": "trvelle",
                            "description": "Trvelle travel planning tools",
                            "tools": functions,
                        }
                    ]
                    if provider == "chatgpt"
                    else functions
                )
            async with client.stream(
                "POST",
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            ) as response:
                if response.is_error:
                    raw = await response.aread()
                    raise ProviderError(
                        response.status_code, json.loads(raw), response.headers
                    )
                completed = None
                streamed_items = {}
                text_deltas = []
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        continue
                    event = json.loads(raw)
                    kind = event.get("type")
                    if kind in (
                        "response.output_item.added",
                        "response.output_item.done",
                    ):
                        item = event.get("item", {})
                        streamed_items[
                            item.get(
                                "id",
                                str(event.get("output_index", len(streamed_items))),
                            )
                        ] = item
                    elif kind == "response.output_text.delta":
                        text_deltas.append(event.get("delta", ""))
                    elif kind in (
                        "response.function_call_arguments.delta",
                        "response.function_call_arguments.done",
                    ):
                        item = streamed_items.get(event.get("item_id"))
                        if item is not None:
                            item["arguments"] = (
                                event.get("arguments", "")
                                if kind.endswith(".done")
                                else item.get("arguments", "") + event.get("delta", "")
                            )
                    if kind == "response.completed":
                        completed = event["response"]
                        break
                    elif event.get("type") in (
                        "response.failed",
                        "error",
                        "response.incomplete",
                    ):
                        detail = event.get("response", event)
                        raise ProviderError(
                            429 if "usage_limit" in json.dumps(detail) else 503,
                            detail,
                            response.headers,
                        )
                if completed is None:
                    raise ValueError("Provider stream ended without completion")
                blocks = completed.get("output") or list(streamed_items.values())
                response_model = completed.get("model") or model
                result = AIMessage(
                    content="\n".join(
                        b["text"]
                        for item in blocks
                        if item["type"] == "message"
                        for b in item.get("content", [])
                        if b["type"] == "output_text"
                    )
                    or "".join(text_deltas),
                    tool_calls=[
                        {
                            "id": item["call_id"],
                            "name": item["name"],
                            "args": json.loads(item["arguments"]),
                        }
                        for item in blocks
                        if item["type"] == "function_call"
                    ],
                    additional_kwargs={
                        "response_model": response_model,
                        "responses_reasoning": [item for item in blocks if item.get("type") == "reasoning" and item.get("encrypted_content")],
                        "reasoning_scope": {"provider": provider, "model": model},
                    },
                    usage_metadata={
                        'input_tokens': completed.get('usage', {}).get('input_tokens', 0),
                        'output_tokens': completed.get('usage', {}).get('output_tokens', 0),
                        'total_tokens': completed.get('usage', {}).get('total_tokens', 0),
                        'input_token_details': {'cache_read': completed.get('usage', {}).get('input_tokens_details', {}).get('cached_tokens', 0)},
                        'output_token_details': {'reasoning': completed.get('usage', {}).get('output_tokens_details', {}).get('reasoning_tokens', 0)},
                    },
                    id=completed.get("id"),
                )
                if (
                    provider == "chatgpt"
                    and (result.content or result.tool_calls)
                    and (response_model == model or response_model.startswith(model + "-"))
                ):
                    self.remember_chatgpt_model(owner, model)
                return result
