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
    await db._write("INSERT INTO oauth_states VALUES ('old', 'v', 1)")
    assert await db.pop_oauth_state("old") is None        # expired

    tid = await db.create_tenant(10)
    assert (await db.get_tenant(tid))["status"] == "draft"
    await db.update_tenant(tid)                           # no fields: no-op, not a SQL error
    assert (await db.get_tenant_by_owner(10))["id"] == tid

    await db.upsert_user(11, "Reza", "reza")
    other = await db.create_tenant(11)

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
    for bad in ("x.y.z", _jwt([1]), _jwt({**good, "exp": None}), _jwt({**good, "exp": "soon"})):
        try:
            oidc.claims_from_id_token(bad, now=1000)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
    oidc.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(200, json={"access_token": "a"}))
    try:
        await oidc.exchange("c", "v")
        raise AssertionError("missing id_token must raise")
    except ValueError:
        pass
    finally:
        oidc.TRANSPORT = None


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
    or_state = {"limit": 0.0, "patches": [], "fail_patch": False}   # patches = successful limit PATCHes

    def or_handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content or b"{}")
        or_calls.append((req.method, body))
        if req.method == "POST":
            or_state["limit"] = body["limit"]
            return httpx.Response(200, json={"key": "sk-or-v1-t", "data": {"hash": "hk"}})
        if req.method == "GET":
            return httpx.Response(200, json={"data": {"limit": or_state["limit"], "usage": 0}})
        if "limit" in body:
            if or_state["fail_patch"]:
                return httpx.Response(500, json={"error": {"message": "down"}})
            or_state["limit"] = body["limit"]
            or_state["patches"].append(body)
        return httpx.Response(200, json={"data": {}})

    openrouter.TRANSPORT = httpx.MockTransport(or_handler)
    tg.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(200, json={
        "ok": True, "result": {"id": 777, "username": "sara_bot", "can_connect_to_business": True}}))

    await db.upsert_user(20, "Sara", "sara")
    tid = await db.create_tenant(20)
    assert not hasattr(tenants, "attach_bot")
    await tenants.save_profile(tid, "Sara", "I study law", "short, lowercase", "never promise money")
    env = (tenants.tenant_dir(tid) / ".env").read_text(encoding="utf-8")
    assert "TG_BOT_TOKEN='1:platform'" in env and "OWNER_USER_ID='20'" in env and "HOSTED='1'" in env
    assert "OWNER_FIRST_NAME='Sara'" in env, env
    if os.name == "posix":
        assert stat.S_IMODE((tenants.tenant_dir(tid) / ".env").stat().st_mode) == 0o600
    personas = {p.name for p in (tenants.tenant_dir(tid) / "prompts" / "personas").iterdir()}
    assert personas and all(n.endswith(".example.txt") for n in personas), personas  # never real personas
    about = (tenants.tenant_dir(tid) / "prompts" / "about_me.txt").read_text(encoding="utf-8")
    assert "I study law" in about and "never promise money" in about
    assert (await db.get_tenant(tid))["profile_done"] == 1

    # Payment applied once even when activation fails: pm2 down on first top_up, retry same ref.
    def failing_run(args, **kw):
        import subprocess
        PM2_CALLS.append(args[1:])
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")
    await db.update_tenant(tid, status="awaiting_credit")
    pm2.RUN = failing_run
    try:
        await tenants.top_up(tid, 3.0, "300k", "", 1, "r-1")
        raise AssertionError("pm2 failure must raise")
    except pm2.Pm2Error:
        pass
    t = await db.get_tenant(tid)
    assert t["or_key_hash"] == "hk" and t["status"] != "running"
    assert (await db.get_payment("r-1"))["applied"] == 1
    assert or_calls[0] == ("POST", {"name": "tgs-%d" % tid, "limit": 0.0, "limit_reset": None}), or_calls
    pm2.RUN = fake_run
    assert await tenants.top_up(tid, 3.0, "300k", "", 1, "r-1") is False       # retry: no new row
    assert [m for m, _ in or_calls].count("POST") == 1, or_calls      # one key only
    assert or_state["patches"] == [{"limit": 3.0}], or_state          # no second credit
    assert (await db.get_tenant(tid))["status"] == "running"
    start = next(c for c in PM2_CALLS if c[0] == "start")
    assert "tgs-%d" % tid in start and "--cwd" in start and "-m" in start and "secretary" in start
    env = (tenants.tenant_dir(tid) / ".env").read_text(encoding="utf-8")
    assert "sk-or-v1-t" in env and "DASHBOARD_PROXY_SECRET=" in env

    # Top-up: limit 3 + 2 = 5, double submit ignored.
    assert await tenants.top_up(tid, 2.0, "200k", "", 1, "r-9") is True
    assert await tenants.top_up(tid, 2.0, "200k", "", 1, "r-9") is False
    assert or_state["patches"] == [{"limit": 3.0}, {"limit": 5.0}], or_state

    # OpenRouter PATCH fails first: raises, retry applies old+amount exactly once.
    await db.upsert_user(22, "Neda", None)
    t3 = await db.create_tenant(22)
    await db.update_tenant(t3, profile_done=1)
    or_state["fail_patch"] = True
    try:
        await tenants.top_up(t3, 4.0, "400k", "", 1, "r-fail")
        raise AssertionError("PATCH failure must raise")
    except openrouter.OpenRouterError:
        pass
    assert (await db.get_payment("r-fail"))["applied"] == 0
    or_state["fail_patch"] = False
    n = len(or_state["patches"])
    await tenants.top_up(t3, 4.0, "400k", "", 1, "r-fail")
    assert or_state["patches"][n:] == [{"limit": 4.0}], or_state
    assert (await db.get_payment("r-fail"))["applied"] == 1
    await tenants.top_up(t3, 4.0, "400k", "", 1, "r-fail")
    assert len(or_state["patches"]) == n + 1

    # Unknown tenant / profile not done: ValueError, no payment row.
    for bad in (9999, (await db.create_tenant(23))):
        try:
            await tenants.top_up(bad, 1.0, "", "", 1, "r-bad%d" % bad)
            raise AssertionError("must refuse")
        except ValueError:
            assert await db.get_payment("r-bad%d" % bad) is None

    # pm2.start on an existing process restarts instead of double-starting.
    def listed_run(args, **kw):
        import subprocess
        PM2_CALLS.append(args[1:])
        out = json.dumps([{"name": "tgs-5", "pm2_env": {"status": "online", "restart_time": 0}}]) \
            if args[1] == "jlist" else ""
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")
    pm2.RUN = listed_run
    PM2_CALLS.clear()
    pm2.start(5, tenants.tenant_dir(tid))
    assert ["restart", "tgs-5"] in PM2_CALLS and not any(c[0] == "start" for c in PM2_CALLS), PM2_CALLS
    pm2.RUN = fake_run

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


from hosting import app as happ  # noqa: E402

BASE = "https://host.test"


def web(app, cookie: str | None = None) -> httpx.AsyncClient:
    headers = {"origin": BASE}
    if cookie:
        headers["cookie"] = f"{happ.COOKIE}={cookie}"
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE, headers=headers)


@check
async def check_routes() -> None:
    pm2.RUN = fake_run
    app = happ.create_app()
    async with web(app) as c:
        assert (await c.get("/")).status_code == 200
        r = await c.get("/account")
        assert r.status_code == 303 and r.headers["location"] == "/login"
        r = await c.get("/login")
        assert r.headers["location"].startswith("https://oauth.telegram.org/auth?")
        state = r.headers["location"].split("state=")[1].split("&")[0]

    # Callback with a stubbed token endpoint.
    claims = {"iss": "https://oauth.telegram.org", "aud": "cid", "exp": 4e9, "id": 30, "name": "Mina",
              "preferred_username": "mina"}
    oidc.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(200, json={"id_token": _jwt(claims)}))
    async with web(app) as c:
        r = await c.get(f"/auth/callback?code=abc&state={state}")
        assert r.status_code == 303 and r.headers["location"] == "/account"
        cookie = r.cookies[happ.COOKIE]
        r = await c.get(f"/auth/callback?code=abc&state={state}")      # state is one-time
        assert r.status_code == 400
    oidc.TRANSPORT = None

    async with web(app, cookie) as c:
        # Resume logic.
        assert await happ.next_step(30) == "consent"
        assert (await c.get("/account")).headers["location"] == "/onboard/consent"
        form = {"first_name": "Mina", "about": "x", "style": "", "never": ""}
        assert (await c.post("/onboard/profile", data=form)).status_code == 303
        assert await db.get_tenant_by_owner(30) is None                # no tenant without consent
        await c.post("/onboard/consent", data={"accept": "1"})
        assert await happ.next_step(30) == "profile"

        await c.post("/onboard/profile", data=form)
        assert await happ.next_step(30) == "payment"                   # trial credit is 0
        assert (await db.get_tenant_by_owner(30))["status"] == "awaiting_credit"

        # Non-admin cannot reach admin.
        assert (await c.get("/admin")).status_code == 403
        # Cross-site POST refused.
        r = await c.post("/onboard/consent", data={"accept": "1"}, headers={"origin": "https://evil.test"})
        assert r.status_code == 403


@check
async def check_onboarding_v2() -> None:
    app = happ.create_app()
    await db.upsert_user(120, "Nil", "nil")
    cookie = await db.create_session(120)
    async with web(app, cookie) as c:
        assert await happ.next_step(120) == "consent"
        await c.post("/onboard/consent", data={"accept": "1"})
        assert await happ.next_step(120) == "profile"
        r = await c.get("/onboard/bot")                       # old URL
        assert r.status_code == 303 and r.headers["location"] == "/account"
        await c.post("/onboard/profile", data={"first_name": "Nil", "about": "x", "style": "", "never": ""})
        assert await happ.next_step(120) == "payment"
        t = await db.get_tenant_by_owner(120)
        env = (tenants.tenant_dir(t["id"]) / ".env").read_text(encoding="utf-8")
        for k in ("TG_BOT_TOKEN='1:platform'", "OWNER_USER_ID='120'", "HOSTED='1'",
                  "DASHBOARD_ROOT_PATH='/app'", "DASHBOARD_LANG='fa'"):
            assert k in env, k
        r = await c.get("/account")
        assert "@plat_bot" in r.text and "Chat Automation" in r.text
        await db.update_tenant(t["id"], status="running")
        r = await c.get("/account")
        assert "@plat_bot" in r.text and "Chat Automation" in r.text
    assert not hasattr(tenants, "attach_bot")


@check
async def check_pay_before_profile() -> None:
    """Admin cannot credit a tenant whose profile is unfinished; paying after the profile starts it."""
    pm2.RUN = fake_run
    or_state = {"limit": 0.0}

    def or_handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content or b"{}")
        if req.method == "POST":
            return httpx.Response(200, json={"key": "sk-or-v1-z", "data": {"hash": "hz"}})
        if req.method == "GET":
            return httpx.Response(200, json={"data": {"limit": or_state["limit"], "usage": 0}})
        or_state["limit"] = body.get("limit", or_state["limit"])
        return httpx.Response(200, json={"data": {}})

    openrouter.TRANSPORT = httpx.MockTransport(or_handler)
    app = happ.create_app()
    await db.upsert_user(1, "Admin", "admin")
    await db.upsert_user(31, "Zoya", "zoya")
    await db.add_consent(31, settings.consent_version)
    tid = await db.create_tenant(31)
    async with web(app, await db.create_session(1)) as c:
        r = await c.post("/admin/payment", data={"tenant_id": tid, "amount_usd": "۵", "client_ref": "z-1"})
        assert r.status_code == 303 and "err=" in r.headers["location"], r.headers["location"]
        assert await db.get_payment("z-1") is None
        r = await c.post("/admin/payment", data={"tenant_id": 99999, "amount_usd": "5", "client_ref": "z-2"})
        assert "err=" in r.headers["location"]                          # unknown tenant: flash, not 500
        assert (await c.get("/admin")).status_code == 200
    async with web(app, await db.create_session(31)) as c:
        await c.post("/onboard/profile", data={"first_name": "Zoya", "about": "x", "style": "", "never": ""})
    assert (await db.get_tenant(tid))["status"] == "awaiting_credit"
    async with web(app, await db.create_session(1)) as c:
        r = await c.post("/admin/payment", data={"tenant_id": tid, "amount_usd": "۵", "client_ref": "z-1"})
        assert "msg=" in r.headers["location"], r.headers["location"]
    t = await db.get_tenant(tid)
    assert t["or_key_hash"] == "hz" and t["status"] == "running" and or_state["limit"] == 5.0
    openrouter.TRANSPORT = None


@check
async def check_hardening() -> None:
    import subprocess
    pm2.RUN = fake_run
    patches: list[dict] = []
    or_state = {"limit": 0.0}

    def or_handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content or b"{}")
        if req.method == "POST":
            return httpx.Response(200, json={"key": "sk-or-v1-h", "data": {"hash": "hh"}})
        if req.method == "GET":
            return httpx.Response(200, json={"data": {"limit": or_state["limit"], "usage": 0}})
        if "limit" in body:
            or_state["limit"] = body["limit"]
            patches.append(body)
        return httpx.Response(200, json={"data": {}})

    openrouter.TRANSPORT = httpx.MockTransport(or_handler)
    app = happ.create_app()
    form = {"first_name": "N", "about": "x", "style": "", "never": ""}

    # 1. Malformed id claim: 400 and no session cookie, not a 500.
    claims = {"iss": "https://oauth.telegram.org", "aud": "cid", "exp": 4e9, "id": "abc", "name": "Bad"}
    oidc.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(200, json={"id_token": _jwt(claims)}))
    async with web(app) as c:
        loc = (await c.get("/login")).headers["location"]
        state = loc.split("state=")[1].split("&")[0]
        r = await c.get(f"/auth/callback?code=abc&state={state}")
        assert r.status_code == 400 and happ.COOKIE not in r.cookies, r.status_code
    oidc.TRANSPORT = None

    # 2 + 3. Stopped tenant keeps its state on profile edit; stale consent bounces to consent.
    await db.upsert_user(41, "Sina", None)
    await db.add_consent(41, settings.consent_version)
    t41 = await db.create_tenant(41)
    await tenants.ensure_key(t41)
    await db.update_tenant(t41, status="stopped", profile_done=1)
    cookie41 = await db.create_session(41)
    PM2_CALLS.clear()
    async with web(app, cookie41) as c:
        r = await c.post("/onboard/profile", data=form)
        assert r.status_code == 303
        assert (await db.get_tenant(t41))["status"] == "stopped"
        assert not any(call[0] in ("start", "restart") for call in PM2_CALLS), PM2_CALLS
        old = settings.consent_version
        settings.consent_version = old + 1
        try:
            r = await c.post("/onboard/profile", data=form)
            assert r.headers["location"] == "/onboard/consent", r.headers["location"]
            r = await c.get("/onboard/profile")
            assert r.headers["location"] == "/onboard/consent", r.headers["location"]
        finally:
            settings.consent_version = old

    # 5 + 6 + 8. Admin payment form: duplicate, bad amounts, bad tenant id.
    await db.upsert_user(43, "Pay", None)
    t43 = await db.create_tenant(43)
    await db.update_tenant(t43, profile_done=1)
    async with web(app, await db.create_session(1)) as c:
        pay = {"tenant_id": str(t43), "amount_usd": "5", "client_ref": "h-1"}
        first = (await c.post("/admin/payment", data=pay)).headers["location"]
        second = (await c.post("/admin/payment", data=pay)).headers["location"]
        assert "msg=" in first and "msg=" in second and first != second   # second says duplicate
        n = await db._one("SELECT COUNT(*) AS n FROM payments WHERE client_ref='h-1'")
        assert n["n"] == 1 and patches == [{"limit": 5.0}], patches
        for i, bad in enumerate(("0", "-3", "1001")):
            r = await c.post("/admin/payment", data={**pay, "amount_usd": bad, "client_ref": f"h-bad{i}"})
            assert "err=" in r.headers["location"], bad
            assert await db.get_payment(f"h-bad{i}") is None
        r = await c.post("/admin/payment", data={**pay, "tenant_id": "abc", "client_ref": "h-x"})
        assert r.status_code == 303 and "err=" in r.headers["location"]

        # 7. pm2 failures on restart/stop are flashed and stored, not 500.
        await db.update_tenant(t43, status="running")

        def failing_run(args, **kw):
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")
        pm2.RUN = failing_run
        for action in ("stop", "restart"):
            r = await c.post(f"/admin/tenant/{t43}/{action}")
            assert r.status_code == 303 and "err=" in r.headers["location"], (action, r.headers["location"])
        t = await db.get_tenant(t43)
        assert t["status"] == "running" and t["last_error"], t
    pm2.RUN = fake_run
    openrouter.TRANSPORT = None


@check
async def check_amounts() -> None:
    assert happ.parse_amount("5") == 5.0
    assert happ.parse_amount("۵") == 5.0
    assert happ.parse_amount("۱۲.۵") == 12.5
    assert happ.parse_amount("۱۲٫۵") == 12.5          # Persian decimal separator
    for bad in ("", "abc", "-3", "0", "1e9"):
        assert happ.parse_amount(bad) is None, bad


from hosting import proxy  # noqa: E402


@check
async def check_proxy() -> None:
    app = happ.create_app()
    await db.upsert_user(40, "P", None)
    tid = await db.create_tenant(40)
    cookie = await db.create_session(40)
    async with web(app, cookie) as c:
        r = await c.get("/app/")                                   # not running yet
        assert r.status_code == 303 and r.headers["location"] == "/account"

    await db.update_tenant(tid, status="running", proxy_secret="sek")
    port = await db.alloc_port(tid)
    seen = []

    def upstream(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(303, headers={"location": "/app/settings?msg=ok", "set-cookie": "x=1"})

    proxy.TRANSPORT = httpx.MockTransport(upstream)
    async with web(app, cookie) as c:
        r = await c.post("/app/settings/config?a=1", data={"HISTORY_TURNS": "9"})
        assert r.status_code == 303 and r.headers["location"] == "/app/settings?msg=ok"
        assert "x=1" not in r.headers.get("set-cookie", "")
    req = seen[-1]
    assert str(req.url) == f"http://127.0.0.1:{port}/settings/config?a=1"
    assert req.headers["x-platform-auth"] == "sek"
    assert b"HISTORY_TURNS=9" in req.content
    assert "cookie" not in req.headers                              # platform session never leaks
    async with web(app, cookie) as c:
        await c.get("/app/http://evil.test/x")                      # path can't redirect the target host
        r = await c.get("/app")
        assert r.status_code == 303 and r.headers["location"] == "/app/"
    assert seen[-1].url.host == "127.0.0.1" and seen[-1].url.port == port

    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")
    proxy.TRANSPORT = httpx.MockTransport(down)
    async with web(app, cookie) as c:
        r = await c.get("/app/")
        assert r.status_code == 503 and "ربات" in r.text           # Persian "starting" page
    proxy.TRANSPORT = None

    async with web(app) as c:                                       # no session
        assert (await c.get("/app/")).status_code == 303


from hosting import bot as pbot  # noqa: E402


@check
async def check_platform_bot() -> None:
    calls = []
    tg.TRANSPORT = httpx.MockTransport(lambda r: (
        calls.append((r.url.path.rsplit("/", 1)[1], json.loads(r.content or b"{}"))),
        httpx.Response(200, json={"ok": True, "result": True}))[1])
    await db.upsert_user(50, "N", None)
    tid = await db.create_tenant(50)

    # Stranger /start: platform answers with the site link.
    await pbot.handle_update({"update_id": 1, "message": {"message_id": 1, "date": 0, "text": "/start",
                              "chat": {"id": 5000, "type": "private"}, "from": {"id": 5000, "is_bot": False, "first_name": "S"}}})
    assert [b["chat_id"] for m, b in calls if m == "sendMessage"] == [5000]
    assert "message" in pbot.ALLOWED_UPDATES and "managed_bot" not in pbot.ALLOWED_UPDATES

    # Credit warning fires once below 20 %.
    await db.update_tenant(tid, status="running", or_key_hash="hw")
    openrouter.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(
        200, json={"data": {"limit": 10, "usage": 9, "limit_remaining": 1}}))
    calls.clear()
    await pbot.check_credit_once()
    await pbot.check_credit_once()
    assert [b.get("chat_id") for m, b in calls if m == "sendMessage"].count(50) == 1  # other fixtures may warn too
    openrouter.TRANSPORT = None
    tg.TRANSPORT = None


def _or_mock(state: dict):
    """OpenRouter mock: records every limit PATCH body; state['timeout_once'] -> apply then time out."""
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content or b"{}")
        state.setdefault("n", 0)
        if req.method == "POST":
            state["n"] += 1
            return httpx.Response(200, json={"key": f"sk-or-v1-{state['n']}", "data": {"hash": f"hx{state['n']}"}})
        if req.method == "GET":
            return httpx.Response(200, json={"data": {"limit": 0, "usage": 0}})
        if state.get("fail_all"):
            return httpx.Response(500, json={"error": {"message": "down"}})
        if "limit" in body:
            state.setdefault("patches", []).append(body["limit"])
            if state.pop("timeout_once", False):
                raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json={"data": {}})
    return handler


from hosting import router  # noqa: E402


@check
async def check_router() -> None:
    sent_tg, fwd = [], []
    tg.TRANSPORT = httpx.MockTransport(lambda r: (
        sent_tg.append((r.url.path.rsplit("/", 1)[1], json.loads(r.content or b"{}"))),
        httpx.Response(200, json={"ok": True, "result":
            {"id": "c-late", "user": {"id": 81}, "user_chat_id": 81, "date": 0, "can_reply": True, "is_enabled": True}
            if r.url.path.endswith("getBusinessConnection") else True}))[1])

    def upstream(req: httpx.Request) -> httpx.Response:
        fwd.append((req.url.port, req.headers.get("x-platform-auth"), json.loads(req.content)))
        return httpx.Response(200, text="ok")
    router.TRANSPORT = httpx.MockTransport(upstream)

    await db.upsert_user(81, "Run", None)
    tid = await db.create_tenant(81)
    await db.update_tenant(tid, status="running", proxy_secret="s81")
    port = await db.alloc_port(tid)

    conn = {"update_id": 1, "business_connection": {"id": "c81", "user": {"id": 81}, "user_chat_id": 81,
            "date": 0, "can_reply": True, "is_enabled": True}}
    assert await router.dispatch(conn) == "forwarded"
    assert fwd[-1] == (port, "s81", conn)
    msg = {"update_id": 2, "business_message": {"message_id": 5, "date": 0, "business_connection_id": "c81",
           "chat": {"id": 900, "type": "private"}, "from": {"id": 900, "is_bot": False, "first_name": "F"}, "text": "hi"}}
    assert await router.dispatch(msg) == "forwarded" and fwd[-1][2]["update_id"] == 2
    # Unknown connection id: resolved via getBusinessConnection, then cached.
    late = {"update_id": 3, "deleted_business_messages": {"business_connection_id": "c-late",
            "chat": {"id": 900, "type": "private"}, "message_ids": [5]}}
    assert await router.dispatch(late) == "forwarded"
    n = len([m for m, _ in sent_tg if m == "getBusinessConnection"])
    assert await router.dispatch({**late, "update_id": 4}) == "forwarded"
    assert len([m for m, _ in sent_tg if m == "getBusinessConnection"]) == n   # cached
    # Owner DM and button go to the owner's tenant.
    dm = {"update_id": 5, "message": {"message_id": 9, "date": 0, "chat": {"id": 81, "type": "private"},
          "from": {"id": 81, "is_bot": False, "first_name": "Run"}, "text": "/pause"}}
    assert await router.dispatch(dm) == "forwarded"
    cb = {"update_id": 6, "callback_query": {"id": "q", "chat_instance": "x", "from": {"id": 81, "is_bot": False, "first_name": "Run"}, "data": "a"}}
    assert await router.dispatch(cb) == "forwarded"

    # Stranger: never forwarded; one DM only.
    before = len(fwd)
    s_conn = {"update_id": 7, "business_connection": {"id": "c99", "user": {"id": 99}, "user_chat_id": 99,
              "date": 0, "can_reply": True, "is_enabled": True}}
    assert await router.dispatch(s_conn) == "dropped"
    assert await router.dispatch({**s_conn, "update_id": 8}) == "dropped"
    s_msg = {**msg, "update_id": 9, "business_message": {**msg["business_message"], "business_connection_id": "c99"}}
    assert await router.dispatch(s_msg) == "dropped"
    assert len(fwd) == before
    assert [b["chat_id"] for m, b in sent_tg if m == "sendMessage"] == [99]
    # Stranger /start goes to the platform handler.
    s_dm = {**dm, "update_id": 10, "message": {**dm["message"], "from": {"id": 99, "is_bot": False, "first_name": "S"},
            "chat": {"id": 99, "type": "private"}, "text": "/start"}}
    assert await router.dispatch(s_dm) == "platform"

    # Tenant not running: dropped.
    await db.update_tenant(tid, status="stopped")
    assert await router.dispatch({**msg, "update_id": 11}) == "dropped"
    await db.update_tenant(tid, status="running")

    # Tenant down: forward fails, returns quickly, next update still routed.
    def down(req): raise httpx.ConnectError("refused")
    router.TRANSPORT = httpx.MockTransport(down)
    assert await router.dispatch({**msg, "update_id": 12}) == "dropped"
    router.TRANSPORT = httpx.MockTransport(upstream)
    assert await router.dispatch({**msg, "update_id": 13}) == "forwarded"
    tg.TRANSPORT = None
    router.TRANSPORT = None


@check
async def check_delete_best_effort() -> None:
    import subprocess
    app = happ.create_app()
    await db.upsert_user(61, "Del", None)
    tid = await db.create_tenant(61)
    tenants.write_env(tid, {"TG_BOT_TOKEN": "961:secret"})
    await db.update_tenant(tid, or_key_hash="hdel")
    openrouter.TRANSPORT = httpx.MockTransport(_or_mock({"fail_all": True}))
    tg.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(400, json={"ok": False, "description": "nope"}))
    pm2.RUN = lambda args, **kw: subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")
    async with web(app, await db.create_session(61)) as c:
        r = await c.post("/account/delete", data={"confirm": "حذف"})
        assert r.status_code == 303, r.status_code
    assert not tenants.tenant_dir(tid).exists()
    assert await db.get_tenant_by_owner(61) is None
    pm2.RUN = fake_run
    openrouter.TRANSPORT = None
    tg.TRANSPORT = None


@check
async def check_idempotent_credit() -> None:
    pm2.RUN = fake_run
    st: dict = {}
    openrouter.TRANSPORT = httpx.MockTransport(_or_mock(st))
    await db.upsert_user(62, "Pay2", None)
    tid = await db.create_tenant(62)
    await db.update_tenant(tid, profile_done=1)
    st["timeout_once"] = True                                       # PATCH lands, response lost
    try:
        await tenants.top_up(tid, 3.0, "", "", 1, "i-1")
        raise AssertionError("timeout must raise")
    except httpx.TimeoutException:
        pass
    await tenants.top_up(tid, 3.0, "", "", 1, "i-1")                # retry same ref
    assert st["patches"][-1] == 3.0, st                             # absolute, not 6.0
    await tenants.top_up(tid, 2.0, "", "", 1, "i-2")
    assert st["patches"][-1] == 5.0, st
    assert await db.payments_total(tid) == 5.0
    assert not hasattr(tenants, "add_credit")

    # Trial: one per user, even after delete + re-onboard.
    settings.trial_credit_usd = 2.0
    try:
        app = happ.create_app()
        form = {"first_name": "T", "about": "x", "style": "", "never": ""}
        await db.upsert_user(63, "Trial", None)
        await db.add_consent(63, settings.consent_version)
        for i, expect in enumerate((2.0, 0.0)):
            t = await db.create_tenant(63)
            async with web(app, await db.create_session(63)) as c:
                assert (await c.post("/onboard/profile", data=form)).status_code == 303
            assert st["patches"][-1] == expect, (i, st)
            await tenants.delete(t)
    finally:
        settings.trial_credit_usd = 0.0
    openrouter.TRANSPORT = None


@check
async def check_minor_fixes() -> None:
    import subprocess
    # 4. absolute tenant dir
    old_root = settings.tenants_root
    settings.tenants_root = Path("rel_tenants")
    try:
        assert tenants.tenant_dir(7).is_absolute()
    finally:
        settings.tenants_root = old_root

    await db.upsert_user(64, "Over", None)
    tid = await db.create_tenant(64)

    # 6. pm2 subprocess env carries no hosting secrets
    seen = {}

    def spy(args, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")
    pm2.RUN = spy
    pm2.status()
    env = seen["env"]
    assert "OPENROUTER_MGMT_KEY" not in env and "DB_PATH" not in env and "PLATFORM_BOT_TOKEN" not in env
    assert "PATH" in env or "Path" in env
    pm2.RUN = fake_run

    # 7. missing limit_remaining: no spurious warning
    sent = []
    tg.TRANSPORT = httpx.MockTransport(lambda r: (sent.append(r.url.path), httpx.Response(200, json={"ok": True, "result": True}))[1])
    openrouter.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": {"limit": 10, "usage": 0}}))
    await db.update_tenant(tid, status="running", or_key_hash="hnone", warned_at_limit=None)
    await pbot.check_credit_once()
    assert not [p for p in sent if p.endswith("sendMessage")], sent
    openrouter.TRANSPORT = None
    tg.TRANSPORT = None


def _widget_params(**fields) -> dict:
    """Sign fields the way Telegram's Login Widget does (key = sha256(bot token))."""
    import hashlib
    import hmac
    data = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hashlib.sha256(settings.platform_bot_token.encode()).digest()
    return {**fields, "hash": hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()}


@check
async def check_login_widget() -> None:
    import time
    now = int(time.time())
    good = _widget_params(id="70", first_name="Sara", username="sara", auth_date=str(now))
    assert oidc.widget_user(good, now)["id"] == "70"
    for bad in ({**good, "id": "71"}, {**good, "hash": "0" * 64}, {k: v for k, v in good.items() if k != "hash"},
                _widget_params(id="70", auth_date=str(now - 90000))):
        try:
            oidc.widget_user(bad, now)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass

    old = settings.oidc_client_id
    settings.oidc_client_id = ""  # no OIDC client: /login shows the widget instead
    try:
        app = happ.create_app()
        async with web(app) as c:
            r = await c.get("/login")
            assert r.status_code == 200 and 'data-telegram-login="plat_bot"' in r.text, r.status_code
            assert 'data-auth-url="https://host.test/auth/telegram"' in r.text
            r = await c.get("/auth/telegram", params={**good, "id": "71"})
            assert r.status_code == 400 and happ.COOKIE not in r.cookies
            r = await c.get("/auth/telegram", params=good)
            assert r.status_code == 303 and r.headers["location"] == "/account"
            cookie = r.cookies[happ.COOKIE]
        assert await db.session_user(cookie) == 70
        assert (await db.get_user(70))["first_name"] == "Sara"
    finally:
        settings.oidc_client_id = old


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
