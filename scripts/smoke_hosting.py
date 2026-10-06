"""Offline checks for the hosting control plane. No Telegram, OpenRouter or pm2.

    PYTHONIOENCODING=utf-8 uv run python scripts/smoke_hosting.py
"""
import asyncio
import base64
import json
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp())
os.environ.update(
    PLATFORM_BOT_TOKEN="1:platform", PLATFORM_BOT_USERNAME="plat_bot",
    OIDC_CLIENT_ID="cid", OIDC_CLIENT_SECRET="csecret",
    OPENROUTER_MGMT_KEY="sk-or-mgmt", PUBLIC_URL="https://host.test", ADMIN_TG_ID="1",
    DB_PATH=str(_tmp / "hosting.db"), TENANTS_ROOT=str(_tmp / "tenants"),
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from hosting import db, oidc, openrouter, tg  # noqa: E402
from hosting.config import settings  # noqa: E402

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


@check
async def check_db() -> None:
    await db.upsert_user(10, "Ali", "ali")
    await db.upsert_user(10, "Ali2", "ali")              # upsert, not duplicate
    assert (await db.get_user(10))["first_name"] == "Ali2"
    assert not await db.has_consent(10, 1)
    await db.add_consent(10, 1)
    assert await db.has_consent(10, 1) and not await db.has_consent(10, 2)

    s = await db.create_session(10)
    assert await db.session_user(s) == 10
    await db.end_session(s)
    assert await db.session_user(s) is None

    await db.put_oauth_state("st", "ver")
    assert await db.pop_oauth_state("st") == "ver"
    assert await db.pop_oauth_state("st") is None         # one-time

    tid = await db.create_tenant(10)
    assert (await db.get_tenant(tid))["status"] == "draft"
    assert (await db.get_tenant_by_owner(10))["id"] == tid

    await db.set_tenant_bot(tid, 555, "ali_bot", managed=False)
    await db.set_tenant_bot(tid, 555, "ali_bot", managed=False)   # same owner re-attach: fine
    await db.upsert_user(11, "Reza", "reza")
    other = await db.create_tenant(11)
    try:
        await db.set_tenant_bot(other, 555, "ali_bot", managed=False)
        raise AssertionError("bot reuse across tenants must fail")
    except db.BotTaken:
        pass

    p1 = await db.alloc_port(tid)
    assert p1 == await db.alloc_port(tid)                 # stable per tenant
    p2 = await db.alloc_port(other)
    assert p1 != p2 and settings.port_range_start <= p2 <= settings.port_range_end

    assert await db.add_payment(tid, 5.0, "500k toman", "", 1, "ref-1") is True
    assert await db.add_payment(tid, 5.0, "500k toman", "", 1, "ref-1") is False   # double submit

    await db.release_tenant(other)                        # deleted tenants free the owner
    assert await db.get_tenant_by_owner(11) is None
    assert await db.create_tenant(11)


def _jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


@check
async def check_openrouter() -> None:
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, json.loads(req.content or b"{}")))
        assert req.headers["authorization"] == "Bearer sk-or-mgmt"
        if req.method == "POST":
            return httpx.Response(200, json={"key": "sk-or-v1-tenant", "data": {"hash": "h1"}})
        if req.method == "GET":
            return httpx.Response(200, json={"data": {"limit": 5, "usage": 1, "limit_remaining": 4, "disabled": False}})
        return httpx.Response(200, json={"data": {}})

    openrouter.TRANSPORT = httpx.MockTransport(handler)
    assert await openrouter.create_key("tgs-1", 5.0) == ("sk-or-v1-tenant", "h1")
    assert seen[-1] == ("POST", "/api/v1/keys", {"name": "tgs-1", "limit": 5.0, "limit_reset": None})
    assert (await openrouter.get_key("h1"))["limit_remaining"] == 4
    await openrouter.set_limit("h1", 10.0)
    assert seen[-1] == ("PATCH", "/api/v1/keys/h1", {"limit": 10.0})
    await openrouter.disable_key("h1")
    assert seen[-1] == ("PATCH", "/api/v1/keys/h1", {"disabled": True})

    openrouter.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(401, json={"error": {"message": "bad"}}))
    try:
        await openrouter.get_key("h1")
        raise AssertionError("401 must raise")
    except openrouter.OpenRouterError:
        pass


@check
async def check_tg() -> None:
    tg.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(
        200, json={"ok": True, "result": {"id": 555, "username": "ali_bot"}}))
    assert (await tg.call("1:x", "getMe"))["id"] == 555
    tg.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(
        401, json={"ok": False, "description": "Unauthorized"}))
    try:
        await tg.call("1:x", "getMe")
        raise AssertionError("must raise")
    except tg.TelegramError as e:
        assert "Unauthorized" in str(e)


@check
async def check_oidc() -> None:
    verifier, challenge = oidc.new_pkce()
    assert 43 <= len(verifier) <= 128 and "=" not in challenge
    url = oidc.auth_url("st8", challenge)
    assert url.startswith("https://oauth.telegram.org/auth?")
    for part in ("client_id=cid", "state=st8", "code_challenge_method=S256", "response_type=code",
                 "redirect_uri=https%3A%2F%2Fhost.test%2Fauth%2Fcallback"):
        assert part in url, part
    good = {"iss": "https://oauth.telegram.org", "aud": "cid", "exp": 2000, "id": 42, "name": "Ali"}
    assert oidc.claims_from_id_token(_jwt(good), now=1000)["id"] == 42
    for bad in ({**good, "iss": "https://evil"}, {**good, "aud": "other"}, {**good, "exp": 999}):
        try:
            oidc.claims_from_id_token(_jwt(bad), now=1000)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass


async def main() -> None:
    await db.init(settings.db_path)
    try:
        for fn in CHECKS:
            await fn()
            print("ok", fn.__name__)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
