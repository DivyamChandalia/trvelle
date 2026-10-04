"""Local, owner-scoped model credentials and ChatGPT PKCE authorization.

Tokens never travel through the website. All persistent secrets are encrypted;
this local installation's master key and credential directories are owner-only.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
from cryptography.fernet import Fernet

PROVIDERS = ("openai", "anthropic", "google", "openrouter", "chatgpt", "claude")
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"


class AccountError(ValueError):
    pass


class ModelAccounts:
    def __init__(self, root=None):
        self.root = Path(
            root or os.getenv("MODEL_ACCOUNTS_DIR", ".runtime/model-accounts")
        ).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        key = self.root / "master.key"
        try:
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as output:
                output.write(Fernet.generate_key())
        os.chmod(key, 0o600)
        self.cipher = Fernet(key.read_bytes())
        self.pending = {}
        self.locks = {}
        self.catalogs = {}

    def directory(self, owner):
        path = self.root / str(uuid.UUID(str(owner)))
        path.mkdir(mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
        return path

    def load(self, owner):
        path = self.directory(owner) / "accounts.enc"
        if not path.exists():
            return {"keys": {}, "roles": {}, "oauth": {}}
        return json.loads(self.cipher.decrypt(path.read_bytes()))

    def save(self, owner, data):
        path = self.directory(owner) / "accounts.enc"
        temporary = path.with_name("write-" + secrets.token_hex(8))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(self.cipher.encrypt(json.dumps(data).encode()))
        os.replace(temporary, path)

    def set_key(self, owner, provider, key):
        if provider not in PROVIDERS[:4]:
            raise AccountError("Unknown API provider")
        if key and (len(key) < 10 or len(key) > 2048 or any(c.isspace() for c in key)):
            raise AccountError("Enter a valid API key without spaces")
        data = self.load(owner)
        if key:
            data["keys"][provider] = key
        else:
            data["keys"].pop(provider, None)
        self.save(owner, data)
        self.catalogs.clear()

    def state(self, owner):
        data = self.load(owner)
        chatgpt = data["oauth"].get("chatgpt", {})
        pending = self.pending.get((str(owner), "chatgpt"), {})
        return {
            "roles": data["roles"],
            "keys": {p: bool(data["keys"].get(p)) for p in PROVIDERS[:4]},
            "accounts": {
                "chatgpt": {
                    "status": pending.get("status")
                    or ("connected" if chatgpt.get("access_token") else "disconnected"),
                    "sharing": "chatgpt.tokens.use.direct" in chatgpt.get("scopes", []),
                    "email": chatgpt.get("email"),
                    "error": pending.get("error"),
                },
                "claude": {"status": "disconnected"},
            },
        }

    async def verify_identity(self, token, client_id, nonce=None, received_at=None):
        async with httpx.AsyncClient(timeout=15) as client:
            discovery = await client.get(
                "https://auth.openai.com/.well-known/openid-configuration"
            )
            discovery.raise_for_status()
            metadata = discovery.json()
            # Never follow a token-supplied issuer/JWKS URL.
            if metadata["issuer"].rstrip("/") != "https://auth.openai.com":
                raise AccountError("Unexpected identity issuer")
            if not metadata["jwks_uri"].startswith("https://auth.openai.com/"):
                raise AccountError("Unexpected identity key service")
            response = await client.get(metadata["jwks_uri"])
            response.raise_for_status()
        kid = jwt.get_unverified_header(token).get("kid")
        keys = [k for k in response.json()["keys"] if k.get("kid") == kid]
        if len(keys) != 1:
            raise AccountError("Identity key could not be verified")
        claims = jwt.decode(
            token,
            jwt.PyJWK.from_dict(keys[0]).key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=metadata["issuer"],
            leeway=5,
            options={
                "require": ["iss", "aud", "exp", "iat", "sub"],
                **({"verify_exp": False, "verify_iat": False} if received_at else {}),
            },
        )
        if received_at and (
            claims["exp"] < received_at - 5 or claims["iat"] > received_at + 5
        ):
            raise AccountError("Expired identity token")
        if not claims.get("sub") or (
            nonce is not None and claims.get("nonce") != nonce
        ):
            raise AccountError("Identity verification failed")
        if claims.get("azp", client_id) != client_id or (
            isinstance(claims["aud"], list)
            and len(claims["aud"]) > 1
            and claims.get("azp") != client_id
        ):
            raise AccountError("Identity audience verification failed")
        return claims

    async def start_chatgpt(self, owner):
        slot = (str(owner), "chatgpt")
        if self.pending.get(slot, {}).get("status") in ("connecting", "verifying"):
            return {"url": self.pending[slot]["url"]}
        previous = self.load(owner)["oauth"].get("chatgpt", {})
        host_path = self.root / "host-id"
        if not host_path.exists():
            fd = os.open(host_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                output.write("urn:uuid:" + str(uuid.uuid4()))
        state, nonce, verifier = [secrets.token_urlsafe(32) for _ in range(3)]
        pending = {"status": "connecting"}
        self.pending[slot] = pending
        server = None

        async def callback(reader, writer):
            try:
                line = await asyncio.wait_for(reader.readline(), 5)
                if len(line) > 8192:
                    raise AccountError("Invalid callback")
                method, target, _ = line.decode().split(" ", 2)
                parsed = urlsplit(target)
                params = parse_qs(parsed.query)

                def param(name):
                    values = params.get(name, [])
                    return values[0] if len(values) == 1 else ""

                if (
                    method != "GET"
                    or parsed.path != "/auth/callback"
                    or not hmac.compare_digest(param("state"), state)
                ):
                    writer.write(
                        b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\nInvalid callback"
                    )
                    return
                if pending["status"] != "connecting":
                    raise AccountError("This sign-in attempt has ended")
                pending["status"] = "verifying"
                pending["callback_task"] = asyncio.current_task()
                server.close()
                if param("error"):
                    raise AccountError(
                        "ChatGPT authorization was declined. You can try again."
                    )
                client_id = param("client_id") or previous.get("client_id")
                if (
                    not client_id
                    or client_id == "dynamic_agent_client"
                    or (
                        previous.get("client_id") and previous["client_id"] != client_id
                    )
                ):
                    raise AccountError("Registration could not be verified")
                # Retain issued registration even if the one-time code exchange fails.
                data = self.load(owner)
                data["oauth"].setdefault("chatgpt", {})["client_id"] = client_id
                self.save(owner, data)
                async with httpx.AsyncClient(timeout=20) as client:
                    result = await client.post(
                        "https://auth.openai.com/api/accounts/oauth/token",
                        data={
                            "grant_type": "authorization_code",
                            "client_id": client_id,
                            "code": param("code"),
                            "code_verifier": verifier,
                            "redirect_uri": redirect,
                            "resource": RESOURCE,
                        },
                    )
                    if result.is_error:
                        raise AccountError(
                            "ChatGPT code exchange failed. Please reconnect."
                        )
                    tokens = result.json()
                claims = await self.verify_identity(
                    tokens.get("id_token", ""), client_id, nonce
                )
                if previous.get("subject") and previous["subject"] != claims["sub"]:
                    raise AccountError(
                        "Reconnect the previously selected ChatGPT account, or disconnect it first."
                    )
                tokens = self.checked_tokens(tokens)
                if (
                    self.pending.get(slot) is not pending
                    or pending["status"] != "verifying"
                ):
                    raise AccountError("Sign-in was canceled")
                data = self.load(owner)
                data["oauth"]["chatgpt"] = dict(
                    tokens,
                    client_id=client_id,
                    subject=claims["sub"],
                    email=claims.get("email"),
                )
                self.save(owner, data)
                pending.update(status="connected", error=None)
                self.catalogs.clear()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nChatGPT connected. Return to Trvelle."
                )
            except Exception:
                pending.update(
                    status="disconnected",
                    error="ChatGPT sign-in failed. Please reconnect.",
                )
                writer.write(
                    b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\nSign-in failed. Return to Trvelle and reconnect."
                )
            finally:
                await writer.drain()
                writer.close()

        server = await asyncio.start_server(callback, "127.0.0.1", 0, limit=8192)
        redirect = (
            f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/auth/callback"
        )
        query = dict(
            client_id=previous.get("client_id", "dynamic_agent_client"),
            response_type="code",
            redirect_uri=redirect,
            scope=SCOPES,
            resource=RESOURCE,
            state=state,
            nonce=nonce,
            code_challenge_method="S256",
            code_challenge=base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode()).digest()
            )
            .decode()
            .rstrip("="),
            ext_agent_host_id=host_path.read_text(),
            prompt="consent",
        )
        if not previous.get("client_id"):
            query["agent_name_hint"] = "Trvelle"
        pending["url"] = "https://auth.openai.com/api/accounts/authorize?" + urlencode(
            query
        )
        pending["server"] = server

        async def expire():
            await asyncio.sleep(600)
            server.close()
            if pending["status"] in ("connecting", "verifying"):
                pending.update(
                    status="disconnected", error="Sign-in timed out. Please reconnect."
                )

        pending["task"] = asyncio.create_task(expire())
        return {"url": pending["url"]}

    @staticmethod
    def checked_tokens(tokens):
        scopes = tokens.get("scope", "").split()
        if (
            tokens.get("token_type", "").lower() != "bearer"
            or not tokens.get("access_token")
            or not isinstance(tokens.get("expires_in"), (float, int))
            or tokens["expires_in"] <= 0
        ):
            raise AccountError("Provider returned incomplete credentials")
        if "offline_access" in scopes and not tokens.get("refresh_token"):
            raise AccountError("Provider did not grant refresh permission")
        return dict(
            tokens, scopes=scopes, expires_at=time.time() + tokens["expires_in"]
        )

    async def chatgpt_token(self, owner):
        async with self.locks.setdefault(str(owner), asyncio.Lock()):
            data = self.load(owner)
            saved = data["oauth"].get("chatgpt", {})
            if "chatgpt.tokens.use.direct" not in saved.get(
                "scopes", []
            ) or not saved.get("access_token"):
                raise AccountError("Connect ChatGPT and authorize plan usage first")
            if (
                not saved.get("pending_refresh")
                and saved["expires_at"] > time.time() + 60
            ):
                return saved["access_token"]
            earliest = saved.get("earliest_refresh_at")
            if earliest:
                from datetime import datetime

                deadline = (
                    float(earliest)
                    if isinstance(earliest, (int, float))
                    else datetime.fromisoformat(
                        earliest.replace("Z", "+00:00")
                    ).timestamp()
                )
                if deadline > time.time():
                    raise AccountError("ChatGPT refresh is awaiting its provider reset")
            tokens = saved.get("pending_refresh")
            if tokens is None:
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.post(
                        "https://auth.openai.com/api/accounts/oauth/token",
                        data={
                            "grant_type": "refresh_token",
                            "client_id": saved["client_id"],
                            "refresh_token": saved["refresh_token"],
                            "resource": RESOURCE,
                        },
                    )
                    if response.is_error:
                        raise AccountError("ChatGPT session expired. Please reconnect.")
                    tokens = response.json()
                saved["pending_refresh_received_at"] = time.time()
                saved["pending_refresh"] = tokens
                data["oauth"]["chatgpt"] = saved
                self.save(owner, data)
            if tokens.get("id_token"):
                claims = await self.verify_identity(
                    tokens["id_token"],
                    saved["client_id"],
                    received_at=saved.get("pending_refresh_received_at"),
                )
                if claims["sub"] != saved["subject"]:
                    raise AccountError("ChatGPT account changed. Please reconnect.")
            fields = self.checked_tokens(tokens)
            saved.update(fields)
            saved.pop("pending_refresh", None)
            saved.pop("pending_refresh_received_at", None)
            self.save(owner, data)
            return saved["access_token"]

    async def disconnect_chatgpt(self, owner):
        pending = self.pending.pop((str(owner), "chatgpt"), {})
        if pending.get("server"):
            pending["server"].close()
        if pending.get("task"):
            pending["task"].cancel()
        data = self.load(owner)
        saved = data["oauth"].get("chatgpt", {})
        if pending.get("callback_task") and not pending["callback_task"].done():
            pending["callback_task"].cancel()
            await asyncio.gather(pending["callback_task"], return_exceptions=True)
        renewable = saved.get("pending_refresh", {}).get("refresh_token") or saved.get(
            "refresh_token"
        )
        if renewable:
            async with httpx.AsyncClient(timeout=15) as client:
                discovery = await client.get(
                    "https://auth.openai.com/.well-known/openid-configuration"
                )
                discovery.raise_for_status()
                endpoint = discovery.json().get("revocation_endpoint", "")
                if not endpoint.startswith("https://auth.openai.com/"):
                    raise AccountError(
                        "Provider revocation endpoint could not be verified"
                    )
                response = await client.post(
                    endpoint,
                    data={
                        "client_id": saved["client_id"],
                        "token": renewable,
                        "token_type_hint": "refresh_token",
                    },
                )
                if response.is_error:
                    raise AccountError(
                        "Provider revocation failed. Retry disconnecting or revoke access in ChatGPT settings."
                    )
        data = self.load(owner)
        data["oauth"].pop("chatgpt", None)
        self.save(owner, data)
        self.catalogs.clear()

    async def close(self):
        tasks = []
        for pending in self.pending.values():
            if pending.get("server"):
                pending["server"].close()
            for name in ("task", "callback_task"):
                if pending.get(name):
                    pending[name].cancel()
                    tasks.append(pending[name])
        await asyncio.gather(*tasks, return_exceptions=True)
