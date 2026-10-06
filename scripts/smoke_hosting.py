"""Offline checks for the hosting control plane. No Telegram, OpenRouter or pm2.

    PYTHONIOENCODING=utf-8 uv run python scripts/smoke_hosting.py
"""
import asyncio
import base64
import json
import os
import stat
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

from hosting import db, oidc, openrouter, pm2, tenants, tg  # noqa: E402
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


PM2_CALLS: list[list[str]] = []


def fake_run(args, **kw):
    PM2_CALLS.append(args[1:])
    import subprocess
    out = "[]" if args[1] == "jlist" else ""
    return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")


@check
async def check_provisioning() -> None:
    pm2.RUN = fake_run
    or_calls = []

    def or_handler(req: httpx.Request) -> httpx.Response:
        or_calls.append((req.method, json.loads(req.content or b"{}")))
        if req.method == "POST":
            return httpx.Response(200, json={"key": "sk-or-v1-t", "data": {"hash": "hk"}})
        if req.method == "GET":
            return httpx.Response(200, json={"data": {"limit": 3.0, "usage": 0, "limit_remaining": 3.0}})
        return httpx.Response(200, json={"data": {}})

    openrouter.TRANSPORT = httpx.MockTransport(or_handler)
    tg.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(200, json={
        "ok": True, "result": {"id": 777, "username": "sara_bot", "can_connect_to_business": True}}))

    await db.upsert_user(20, "Sara", "sara")
    tid = await db.create_tenant(20)
    me = await tenants.attach_bot(await db.get_tenant(tid), "777:tok", managed=False)
    assert me["username"] == "sara_bot"
    env = (tenants.tenant_dir(tid) / ".env").read_text(encoding="utf-8")
    assert "777:tok" in env and "OWNER_USER_ID='20'" in env and "HOSTED='1'" in env
    if os.name == "posix":
        assert stat.S_IMODE((tenants.tenant_dir(tid) / ".env").stat().st_mode) == 0o600
    personas = {p.name for p in (tenants.tenant_dir(tid) / "prompts" / "personas").iterdir()}
    assert personas and all(n.endswith(".example.txt") for n in personas), personas  # never real personas

    await tenants.save_profile(tid, "Sara", "I study law", "short, lowercase", "never promise money")
    about = (tenants.tenant_dir(tid) / "prompts" / "about_me.txt").read_text(encoding="utf-8")
    assert "I study law" in about and "never promise money" in about
    assert (await db.get_tenant(tid))["profile_done"] == 1

    # Activation: pm2 fails the first time -> key kept, retry reuses it.
    def failing_run(args, **kw):
        import subprocess
        PM2_CALLS.append(args[1:])
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")
    pm2.RUN = failing_run
    try:
        await tenants.activate(tid, 3.0)
        raise AssertionError("pm2 failure must raise")
    except pm2.Pm2Error:
        pass
    t = await db.get_tenant(tid)
    assert t["or_key_hash"] == "hk" and t["status"] != "running"
    pm2.RUN = fake_run
    await tenants.activate(tid, 3.0)
    assert [m for m, _ in or_calls].count("POST") == 1, or_calls      # one key only
    assert (await db.get_tenant(tid))["status"] == "running"
    start = next(c for c in PM2_CALLS if c[0] == "start")
    assert "tgs-%d" % tid in start and "--cwd" in start and "-m" in start and "secretary" in start
    env = (tenants.tenant_dir(tid) / ".env").read_text(encoding="utf-8")
    assert "sk-or-v1-t" in env and "DASHBOARD_PROXY_SECRET=" in env

    # Top-up: limit 3 + 2 = 5, double submit ignored.
    assert await tenants.top_up(tid, 2.0, "200k", "", 1, "r-9") is True
    assert await tenants.top_up(tid, 2.0, "200k", "", 1, "r-9") is False
    patches = [b for m, b in or_calls if m == "PATCH"]
    assert patches == [{"limit": 5.0}], patches

    # Another user cannot claim sara_bot.
    await db.upsert_user(21, "X", None)
    other = await db.create_tenant(21)
    try:
        await tenants.attach_bot(await db.get_tenant(other), "777:tok", managed=False)
        raise AssertionError("must refuse")
    except db.BotTaken:
        pass

    # Connected check reads the tenant's own secretary.db.
    assert await tenants.is_connected(tid) is False
    import sqlite3
    con = sqlite3.connect(tenants.tenant_dir(tid) / "secretary.db")
    con.execute("CREATE TABLE connections (conn_id TEXT, owner_user_id INTEGER, is_enabled INTEGER)")
    con.execute("INSERT INTO connections VALUES ('c', 20, 1)")
    con.commit()
    con.close()
    assert await tenants.is_connected(tid) is True

    await tenants.delete(tid)
    assert not tenants.tenant_dir(tid).exists()
    assert ["delete", "tgs-%d" % tid] in PM2_CALLS
    assert {"disabled": True} in [b for m, b in or_calls if m == "PATCH"]
    assert await db.get_tenant_by_owner(20) is None


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
