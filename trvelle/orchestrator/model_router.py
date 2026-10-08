"""Role-aware routing with durable provider/model cooldowns and free-only OpenRouter."""
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import yaml
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_google_genai import ChatGoogleGenerativeAI


def free_tool_model(model):
    """Free previews can lack :free; require explicit zero pricing and tool support."""
    pricing = model.get('pricing') or {}
    if 'tools' not in model.get('supported_parameters', []) or not all(k in pricing for k in ('prompt', 'completion')):
        return False
    try:
        if any(float(value) != 0 for value in pricing.values()):
            return False
        expiry = model.get('expiration_date')
        if expiry and datetime.fromisoformat(expiry.replace('Z', '+00:00')).date() <= datetime.now(timezone.utc).date():
            return False
    except (ValueError, TypeError):
        return False
    return True


class ModelsUnavailableError(RuntimeError):
    pass


class ProviderError(RuntimeError):
    def __init__(self, status, payload, headers=None):
        super().__init__(f"Provider request failed ({status})")
        self.status = status
        self.payload = payload
        self.headers = headers or {}


def next_midnight(now, zone):
    local = datetime.fromtimestamp(now, ZoneInfo(zone))
    return datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), tzinfo=local.tzinfo).timestamp()


def reset_time(error, now):
    """Use all explicit hints; never retry sooner than the longest applicable hint."""
    headers = getattr(error, "headers", {}) or {}
    response = getattr(error, "response", None)
    if response is not None:
        headers = response.headers
    headers = {str(k).lower(): str(v) for k, v in headers.items()}
    deadlines = []
    retry = headers.get("retry-after")
    if retry:
        try:
            deadlines.append(now + max(0, float(retry)))
        except ValueError:
            try:
                deadlines.append(parsedate_to_datetime(retry).timestamp())
            except (ValueError, TypeError):
                pass
    reset = headers.get("x-ratelimit-reset")
    if reset:
        try:
            value = float(reset)
            deadlines.append(value / 1000 if value > 1e12 else value)
        except ValueError:
            try:
                deadlines.append(datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp())
            except ValueError:
                pass
    for name, value in headers.items():
        if name != 'x-ratelimit-reset' and ('ratelimit' in name and 'reset' in name):
            try:
                deadlines.append(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp())
            except ValueError:
                units = {'ms': .001, 's': 1, 'm': 60, 'h': 3600}
                matches = re.findall(r'([\d.]+)(ms|s|m|h)', value)
                if matches:
                    deadlines.append(now + sum(float(n)*units[u] for n,u in matches))
    parts = [str(error)]
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(str(current))
        for attr in ("payload", "response_json", "details", "error_details"):
            value = getattr(current, attr, None)
            if value is not None:
                parts.append(json.dumps(value, default=str))
        current = current.__cause__ or current.__context__
    detail = " ".join(parts)
    for delay in re.findall(r'["\']retryDelay["\']\s*:\s*["\']([\d.]+)s', detail):
        deadlines.append(now + float(delay))
    for duration in re.findall(r'(?:retry in|retry after)\s+((?:[\d.]+\s*(?:ms|h|m|s|hours?|minutes?|seconds?)\s*)+)', detail, re.I):
        units = {"h": 3600, "m": 60, "s": 1}
        seconds = 0
        for value, unit in re.findall(r'([\d.]+)\s*(ms|hours?|minutes?|seconds?|h|m|s)', duration, re.I):
            seconds += float(value) * (0.001 if unit == "ms" else units[unit[0].lower()])
        deadlines.append(now + seconds)
    for value in re.findall(r'"(?:reset_at|reset_time|resets_at)"\s*:\s*"([^"]+)"', detail):
        try:
            deadlines.append(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp())
        except ValueError:
            pass
    return max([deadline for deadline in deadlines if deadline > now], default=None), detail


class Cooldowns:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS cooldowns (scope TEXT PRIMARY KEY, until REAL, reason TEXT)")
        self.db.commit()

    def until(self, scope):
        row = self.db.execute("SELECT until FROM cooldowns WHERE scope=?", (scope,)).fetchone()
        return row[0] if row else 0

    def block(self, scope, until, reason):
        self.db.execute("INSERT INTO cooldowns VALUES (?,?,?) ON CONFLICT(scope) DO UPDATE SET until=max(until,excluded.until), reason=excluded.reason", (scope, until, reason))
        self.db.commit()


class ModelRouter:
    def __init__(self, config_path=None, state_path=None):
        config_path = config_path or os.getenv("MODEL_TIERS_CONFIG", str(Path(__file__).parents[1] / "config/model_tiers.yaml"))
        routing = yaml.safe_load(Path(config_path).read_text())
        self.tiers = routing["tiers"]
        self.role_models = routing.get('role_models', {})
        self.cooldowns = Cooldowns(state_path or os.getenv("MODEL_COOLDOWN_DB", ".runtime/model-routing.sqlite3"))
        self.keys = {"google": os.getenv("GOOGLE_API_KEY", ""), "openrouter": os.getenv("OPENROUTER_API_KEY", "")}
        self.locks = {}
        self.clients = {}
        self.personal_routers = {}
        self.catalog = None
        self.catalog_at = 0
        self.catalog_lock = asyncio.Lock()
        self.key_at = 0
        self.key_lock = asyncio.Lock()
        self.free_remaining = None
        self.quota_lock = asyncio.Lock()

    def scope(self, provider, model="*"):
        fingerprint = hashlib.sha256(self.keys[provider].encode()).hexdigest()[:16]
        return f"{provider}:{fingerprint}:{model}"

    def blocked_until(self, provider, model):
        scopes = [self.scope(provider), self.scope(provider, model)]
        if provider == "openrouter":
            scopes.append(self.scope(provider, "free"))
        return max(self.cooldowns.until(scope) for scope in scopes)

    def candidates(self, role, supervisor_tier=0):
        if self.role_models.get(role):
            for entry in self.role_models[role]:
                provider, model = entry.split(':', 1)
                tier = next((i for i, group in enumerate(self.tiers) if entry in group['models']), 0)
                if self.keys.get(provider):
                    yield provider, model, tier
            return
        start = 0 if role == "supervisor" else min(supervisor_tier + 1, len(self.tiers) - 1)
        for tier in range(start, len(self.tiers)):
            for entry in self.tiers[tier]["models"]:
                provider, model = entry.split(":", 1)
                if self.keys.get(provider):
                    yield provider, model, tier

    async def refresh_catalog(self):
        async with self.catalog_lock:
            if self.catalog is not None and time.time() - self.catalog_at < 3600:
                return
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get("https://openrouter.ai/api/v1/models")
                response.raise_for_status()
            self.catalog = {m["id"] for m in response.json()["data"] if free_tool_model(m)}
            self.catalog_at = time.time()

    async def refresh_quota(self):
        async with self.key_lock:
            if time.time() - self.key_at < 60:
                return
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {self.keys['openrouter']}"})
            if response.is_error:
                raise ProviderError(response.status_code, response.json(), response.headers)
            self.free_remaining = response.json().get("data", {}).get("free_model_daily_requests", {}).get("remaining")
            self.key_at = time.time()
            if self.free_remaining == 0:
                self.cooldowns.block(self.scope("openrouter", "free"), next_midnight(time.time(), "UTC"), "daily quota")

    @staticmethod
    def messages_for_openrouter(messages, model):
        result = []
        for message in messages:
            role = "tool" if isinstance(message, ToolMessage) else "assistant" if isinstance(message, AIMessage) else "system" if message.type == "system" else "user"
            item = {"role": role, "content": message.text or ""}
            if role == "tool":
                item["tool_call_id"] = message.tool_call_id
            if role == "assistant" and message.tool_calls:
                item["tool_calls"] = [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["args"])}} for c in message.tool_calls]
                if message.additional_kwargs.get("routing_model") == model:
                    reasoning = message.additional_kwargs.get("reasoning_details")
                    if reasoning:
                        item["reasoning_details"] = reasoning
            result.append(item)
        return result

    @staticmethod
    def messages_for_google(messages):
        # Foreign tool calls lack Gemini thought signatures. Preserve their results as text.
        foreign_ids = set()
        result = []
        for message in messages:
            if isinstance(message, AIMessage) and message.additional_kwargs.get("routing_provider") in ("openrouter", "openai", "anthropic", "chatgpt", "claude"):
                foreign_ids.update(c["id"] for c in message.tool_calls)
                result.append(AIMessage(content=(message.text or "") + ("\nPrevious tool requests: " + json.dumps(message.tool_calls) if message.tool_calls else "")))
            elif isinstance(message, ToolMessage) and message.tool_call_id in foreign_ids:
                result.append(HumanMessage(content=f"Previous tool result ({message.name or message.tool_call_id}): {message.text}"))
            else:
                result.append(message)
        return result

    async def request(self, provider, model, messages, tools):
        if provider == "google":
            if model not in self.clients:
                self.clients[model] = ChatGoogleGenerativeAI(model=model, google_api_key=self.keys[provider], timeout=45, max_retries=0)
            return await self.clients[model].bind_tools(tools).ainvoke(self.messages_for_google(messages))
        await self.refresh_catalog()
        if model not in self.catalog:
            raise ProviderError(404, {"error": {"message": "Model is not currently free and tool-capable"}})
        # Serialize free requests to keep the shared remaining counter accurate.
        async with self.quota_lock:
            await self.refresh_quota()
            if self.blocked_until(provider, model) > time.time():
                raise ModelsUnavailableError("OpenRouter free quota is awaiting reset")
            payload = {"model": model, "messages": self.messages_for_openrouter(messages, model), "max_tokens": 8192}
            if model == 'stealth/space-bunny-alpha':
                payload['reasoning'] = {'effort': 'medium'}
            if tools:
                payload["tools"] = [convert_to_openai_tool(t) for t in tools]
                payload["tool_choice"] = "auto"
                payload["provider"] = {"require_parameters": True}
            async with httpx.AsyncClient(timeout=45) as client:
                response = await client.post("https://openrouter.ai/api/v1/chat/completions", headers={"Authorization": f"Bearer {self.keys[provider]}"}, json=payload)
            body = response.json()
            if response.is_error or body.get("error"):
                raise ProviderError(response.status_code if response.is_error else body["error"].get("code", 503), body, response.headers)
            if self.free_remaining is not None:
                self.free_remaining = max(0, self.free_remaining - 1)
                if self.free_remaining == 0:
                    self.cooldowns.block(self.scope(provider, "free"), next_midnight(time.time(), "UTC"), "daily quota")
            message = body["choices"][0]["message"]
            calls = []
            for call in message.get("tool_calls", []):
                args = json.loads(call["function"]["arguments"])
                if not isinstance(args, dict):
                    raise ValueError("Invalid model tool arguments")
                calls.append({"id": call["id"], "name": call["function"]["name"], "args": args})
            return AIMessage(content=message.get("content") or "", tool_calls=calls, id=body.get("id"), additional_kwargs={"reasoning_details": message.get("reasoning_details", [])})

    def record_error(self, provider, model, error):
        now = time.time()
        until, detail = reset_time(error, now)
        status = getattr(error, "status", None) or getattr(error, "code", None)
        if status is None:
            status = next((n for n in (429, 503, 504, 401, 403, 404, 402) if str(n) in detail), None)
        if isinstance(error, ModelsUnavailableError):
            return
        transient = status in (429, 503, 504, 401, 403, 404, 402) or isinstance(error, (TimeoutError, httpx.HTTPError, ValueError)) or "empty response" in detail
        if not transient:
            raise error
        scope = self.scope(provider, model)
        reason = "temporarily unavailable"
        daily = "RequestsPerDay" in detail or "per day" in detail.lower() or "daily" in detail.lower()
        if status in (401, 403, 402):
            scope = self.scope(provider)
            reason = "credentials or account limit"
        if provider == "openrouter" and status == 429:
            payload = getattr(error, "payload", {})
            metadata = payload.get("error", {}).get("metadata", {}) if isinstance(payload, dict) else {}
            if "provider_code" not in metadata and "provider_name" not in metadata:
                scope = self.scope(provider, "free")
        if until is None:
            until = next_midnight(now, "America/Los_Angeles" if provider == "google" else "UTC") if daily else now + (86400 if status in (401, 403, 402, 404) else 60)
        self.cooldowns.block(scope, until, reason)

    async def invoke(self, role, tools, prompt, supervisor_tier=0):
        messages = prompt.to_messages() if hasattr(prompt, "to_messages") else list(prompt)
        from .personal_models import active_owner, active_choices, get_accounts
        owner = active_owner.get()
        if owner is not None:
            personal = get_accounts()
            selected = await personal.invoke_selected(self, owner, role, messages, tools)
            if selected is not None:
                return selected
            # Explicit user selections above win. Otherwise honor configured defaults
            # before considering connected-account recommendations.
            automatic = None if self.role_models.get(role) else await personal.invoke_auto(self, owner, role, messages, tools)
            if automatic is not None:
                return automatic
            keys = dict(self.keys)
            # Durable runs pin roles only. Credentials must always be read freshly
            # from the owner's vault, including after rotation or key removal.
            keys.update({p:k for p,k in personal.load(owner).get('keys',{}).items() if p in keys})
            if keys != self.keys:
                fingerprint = hashlib.sha256(json.dumps(keys, sort_keys=True).encode()).hexdigest()
                if fingerprint not in self.personal_routers:
                    import copy
                    clone = copy.copy(self)
                    clone.keys, clone.clients, clone.personal_routers = keys, {}, {}
                    clone.free_remaining, clone.key_at = None, 0
                    clone.quota_lock, clone.key_lock = asyncio.Lock(), asyncio.Lock()
                    self.personal_routers[fingerprint] = clone
                token = active_owner.set(None)
                try:
                    return await self.personal_routers[fingerprint].invoke(role, tools, messages, supervisor_tier)
                finally:
                    active_owner.reset(token)
        candidates = list(self.candidates(role, supervisor_tier))
        if role == "researcher" and not self.role_models.get(role):
            # If no lower tier is usable, keep the only available planning model.
            candidates += [(p, m, t) for p, m, t in self.candidates("supervisor") if t == supervisor_tier and (p, m, t) not in candidates]
        for provider, model, tier in candidates:
            if self.blocked_until(provider, model) > time.time():
                continue
            async with self.locks.setdefault((provider, model), asyncio.Lock()):
                if self.blocked_until(provider, model) > time.time():
                    continue
                try:
                    response = await asyncio.wait_for(self.request(provider, model, messages, tools), timeout=65)
                    if not response.content and not response.tool_calls:
                        raise RuntimeError("The model returned an empty response")
                    response.additional_kwargs.update(routing_provider=provider, routing_model=model, routing_tier=tier, routing_degraded=role == "researcher" and tier <= supervisor_tier)
                    return response
                except Exception as error:
                    self.record_error(provider, model, error)
        resets = [self.blocked_until(p, m) for p, m, _ in candidates]
        upcoming = min((r for r in resets if r > time.time()), default=None)
        hint = f" Earliest retry: {datetime.fromtimestamp(upcoming, timezone.utc).isoformat()}." if upcoming else " Check provider configuration."
        raise ModelsUnavailableError("No eligible AI model is available." + hint)

    def status(self):
        return {"role_models": self.role_models, "tiers": [{"tier": i + 1, "name": t["name"], "models": [{"provider": p, "model": m, "configured": bool(self.keys.get(p)), "retry_at": datetime.fromtimestamp(self.blocked_until(p, m), timezone.utc).isoformat() if self.blocked_until(p, m) > time.time() else None} for p, m in (entry.split(":", 1) for entry in t["models"])]} for i, t in enumerate(self.tiers)], "openrouter_free_remaining": self.free_remaining}
