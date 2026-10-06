"""Offline checks for the web dashboard and its DB helpers. No Telegram, no OpenRouter.

    uv run python scripts/smoke_dashboard.py
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

_tmp = Path(tempfile.mkdtemp())
TOKEN = "123456:SMOKEtokenSMOKEtokenSMOKEtoken99"
KEY = "sk-or-v1-smokekeysmokekeysmokekey77"
os.environ.update(
    TG_BOT_TOKEN=TOKEN,
    OPENROUTER_API_KEY=KEY,
    OWNER_USER_ID="111",
    DB_PATH=str(_tmp / "smoke.db"),
    PROMPTS_DIR=str(_tmp / "prompts"),
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from secretary import db  # noqa: E402
import types  # noqa: E402
from http.cookies import SimpleCookie  # noqa: E402

import httpx  # noqa: E402

import socket  # noqa: E402

from secretary.config import settings  # noqa: E402
from secretary.dashboard import auth  # noqa: E402
from secretary.dashboard.app import create_app, make_server, run_server  # noqa: E402

from secretary import setup  # noqa: E402

# Routes call these through the `setup` module, so stubbing here keeps the run offline.
setup.or_key_info = lambda key, timeout=15: {"limit_remaining": 4.2, "usage": 1.0}
setup.or_model_ids = lambda: {"openai/gpt-5.5", "google/gemini-3.1-flash-lite", "a/b"}
setup.tg_get_me = lambda token: {"username": "new_bot", "can_connect_to_business": True}

BASE = "http://127.0.0.1:8780"
ORIGIN = {"origin": BASE}
ENV = _tmp / ".env"
STOPS: list[int] = []


class FakeBot:
    username = "smoke_bot"

    def __init__(self) -> None:
        self.sent: list[tuple[int, str, str | None]] = []

    async def send_message(self, chat_id, text, business_connection_id=None, **_):
        self.sent.append((chat_id, text, business_connection_id))
        return types.SimpleNamespace(message_id=len(self.sent))


BOT = FakeBot()


def client(app, base: str = BASE, **kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base, **kw)


def session_from(r: httpx.Response) -> str:
    return SimpleCookie(r.headers["set-cookie"])[auth.COOKIE].value


async def authed(app) -> httpx.AsyncClient:
    """Client carrying a fresh session cookie and a same-origin Origin header."""
    async with client(app) as c:
        r = await c.post("/login", data={"token": await auth.create_login_token()}, headers=ORIGIN)
    return client(app, headers={"cookie": f"{auth.COOKIE}={session_from(r)}", **ORIGIN})


async def check_db() -> None:
    # One-time pop: the second read gets nothing.
    await db.set_state("dash_login:x", "123")
    assert await db.pop_state("dash_login:x") == "123"
    assert await db.pop_state("dash_login:x") is None

    # Purge drops only expired rows under the exact prefix ('_' is not a wildcard).
    now = int(time.time())
    await db.set_state("dash_session:old", str(now - 1))
    await db.set_state("dash_session:new", str(now + 60))
    await db.set_state("dashXsession:old", str(now - 1))
    assert await db.purge_expired_state("dash_session:", now) == 1
    assert await db.get_state("dash_session:new") and await db.get_state("dashXsession:old")

    # Stats, recent replies, owner-only connections, chats, open drafts.
    await db.upsert_connection(conn_id="c1", owner_user_id=111, owner_chat_id=111,
                               can_reply=True, is_enabled=True, rights={"can_reply": True})
    await db.upsert_connection(conn_id="stranger", owner_user_id=999, owner_chat_id=999,
                               can_reply=True, is_enabled=True, rights=None)
    await db.append_message(conn_id="c1", chat_id=5, role="user", content="hi")
    await db.append_message(conn_id="c1", chat_id=5, role="assistant", content="hey", via_bot=True)
    await db.create_pending(conn_id="c1", chat_id=5, contact_name="Sam", contact_msg="hi", draft="yo")
    await db.create_pending(conn_id="c1", chat_id=5, contact_name="Sam", contact_msg="hi",
                            draft="old", ttl_seconds=-1)
    s = await db.get_stats()
    assert (s["total_chats"], s["replies_today"], s["replies_week"], s["pending"]) == (1, 1, 1, 1)
    assert await db.count_open_pending() == 1
    assert [r["content"] for r in await db.recent_bot_replies(5)] == ["hey"]
    assert [c["conn_id"] for c in await db.list_connections()] == ["c1"]
    assert [c["chat_id"] for c in await db.list_chats()] == [5]
    assert [p["draft"] for p in await db.list_open_pending()] == ["yo"]


async def check_auth(app) -> None:
    async with client(app) as c:
        r = await c.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert (await c.get("/login", headers={"host": "evil.example:8780"})).status_code == 400
        token = await auth.create_login_token()
        assert (await c.post("/login", data={"token": token})).status_code == 403  # no Origin
        r = await c.post("/login", data={"token": token}, headers=ORIGIN)
        assert r.status_code == 303
        session = session_from(r)
        assert await auth.session_valid(session)
        r = await c.post("/login", data={"token": token}, headers=ORIGIN)  # second use
        assert r.status_code == 401
        expired = await auth.create_login_token(ttl=-1)
        assert (await c.post("/login", data={"token": expired}, headers=ORIGIN)).status_code == 401
        r = await c.post("/logout", headers={"cookie": f"{auth.COOKIE}={session}", **ORIGIN})
        assert r.status_code == 303 and not await auth.session_valid(session)
    # Review focus 1: a tunnel on another local port still logs in.
    async with client(app, base="http://127.0.0.1:9000") as c:
        r = await c.post("/login", data={"token": await auth.create_login_token()},
                         headers={"origin": "http://127.0.0.1:9000"})
        assert r.status_code == 303
    assert auth.host_ok("[::1]:8780") and auth.host_ok("localhost")
    assert not auth.host_ok("localhost.evil.com:8780") and not auth.host_ok("")
    assert settings.dashboard_enabled is True and settings.dashboard_port == 8780

    # /dashboard shows the real tunnel command once setup saved it, else a placeholder.
    from secretary.commands import dashboard_text
    assert "<user>@<server>" in dashboard_text("tok") and "/login#t=tok" in dashboard_text("tok")
    settings.dashboard_ssh_hint = "ssh -L 8780:127.0.0.1:8780 -p 7744 ai_user@89.36.137.77"
    text = dashboard_text("tok")
    assert "-p 7744 ai_user@89.36.137.77" in text and "<user>" not in text
    settings.dashboard_ssh_hint = ""

    # Opt-in public bind: the public address passes the Host guard and links use it.
    assert not auth.host_ok("89.36.137.77:8780")
    settings.dashboard_public_url = "http://89.36.137.77:8780"
    assert auth.host_ok("89.36.137.77:8780") and not auth.host_ok("evil.example:8780")
    text = dashboard_text("tok")
    assert "http://89.36.137.77:8780/login#t=tok" in text and "ssh -L" not in text
    async with client(app, base="http://89.36.137.77:8780") as c:
        r = await c.post("/login", data={"token": await auth.create_login_token()},
                         headers={"origin": "http://89.36.137.77:8780"})
        assert r.status_code == 303 and "secure" not in r.headers["set-cookie"].lower()
    # Behind an HTTPS reverse proxy the cookie must be Secure and an https Origin accepted.
    settings.dashboard_public_url = "https://dash.example.com"
    async with client(app, base="https://dash.example.com") as c:
        r = await c.post("/login", data={"token": await auth.create_login_token()},
                         headers={"origin": "https://dash.example.com"})
        assert r.status_code == 303 and "secure" in r.headers["set-cookie"].lower()
    settings.dashboard_public_url = ""
    assert make_server(app, 0, "0.0.0.0").config.host == "0.0.0.0"
    assert make_server(app, 0).config.host == "127.0.0.1"


async def check_port_busy() -> None:
    # Spec: a taken port must not kill the bot; run_server logs and returns.
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    port = blocker.getsockname()[1]
    server = make_server(create_app(BOT, lambda: None, env_path=ENV), port)
    await asyncio.wait_for(run_server(server), timeout=10)
    blocker.close()


async def check_status(app) -> None:
    async with await authed(app) as c:
        r = await c.get("/")
        assert r.status_code == 200, r.text
        assert "@smoke_bot" in r.text and "$4.20 left" in r.text
        assert TOKEN not in r.text and KEY not in r.text
        # Review focus 4: a failing credit lookup must not break the page.
        real = setup.or_key_info

        def boom(key, timeout=15):
            raise httpx.ConnectError("down")

        setup.or_key_info = boom
        r = await c.get("/")
        setup.or_key_info = real
        assert r.status_code == 200 and "unavailable" in r.text


def config_form(**over: str) -> dict[str, str]:
    base = {
        "OPENROUTER_MODEL": settings.openrouter_model,
        "EXTRACTOR_MODEL": settings.extractor_model,
        "WHISPER_MODEL": settings.whisper_model,
        "OWNER_FIRST_NAME": settings.owner_first_name,
        "HISTORY_TURNS": str(settings.history_turns),
        "OWNER_USER_ID": str(settings.owner_user_id),
        "TG_BOT_TOKEN": "",
        "OPENROUTER_API_KEY": "",
    }
    return {**base, **over}


async def check_settings(app) -> None:
    from dotenv import dotenv_values

    async with await authed(app) as c:
        r = await c.get("/settings")
        assert r.status_code == 200
        assert TOKEN not in r.text and KEY not in r.text and setup.mask(TOKEN) in r.text

        # Simple view vs advanced: technical fields only inside the advanced groups.
        import re
        adv = "".join(re.findall(r'<fieldset class="group advanced">.*?</fieldset>', r.text, re.S))
        simple = re.sub(r'<fieldset class="group advanced">.*?</fieldset>', "", r.text, flags=re.S)
        for name in ("away_delay_override", "cooldown_override", "OPENROUTER_MODEL", "EXTRACTOR_MODEL",
                     "WHISPER_MODEL", "HISTORY_TURNS", "TG_BOT_TOKEN", "OPENROUTER_API_KEY", "OWNER_USER_ID"):
            assert f'name="{name}"' in adv and f'name="{name}"' not in simple, name
        for name in ("paused", "approval_mode", "innercircle_gate", "delay_override", "quiet_start", "OWNER_FIRST_NAME"):
            assert f'name="{name}"' in simple, name
        assert "Advanced settings" in r.text and 'id="adv-toggle"' in r.text
        assert "<select" in adv and "data-voice-advanced disabled" in adv  # only the switch submits by default

        # Voice switch: orig carries the effective value, so an untouched switch writes nothing.
        await db.set_state("voice_override", "")
        settings.voice_transcribe = True
        r = await c.get("/settings")
        assert 'name="orig_voice_override" value="on"' in r.text
        on, off = ["off", "on"], ["off"]  # hidden "off" + checkbox "on" when checked; last value wins
        r = await c.post("/settings/live", data={"orig_voice_override": "on", "voice_override": on})
        assert "err" not in r.headers["location"] and await db.get_state("voice_override") == ""
        await c.post("/settings/live", data={"orig_voice_override": "on", "voice_override": off})
        assert await db.get_state("voice_override") == "off"
        r = await c.get("/settings")
        assert 'name="orig_voice_override" value="off"' in r.text
        await c.post("/settings/live", data={"orig_voice_override": "off", "voice_override": on})
        assert await db.get_state("voice_override") == "on"
        # Advanced three-way select still accepts "" (back to default).
        await c.post("/settings/live", data={"orig_voice_override": "on", "voice_override": ""})
        assert await db.get_state("voice_override") == ""

        # Live group writes the same bot_state keys the /commands use.
        r = await c.post("/settings/live", data={
            "paused": "on", "voice_override": "off", "quiet_start": "23:00", "quiet_end": "08:00",
            "delay_override": "45", "away_delay_override": "", "cooldown_override": "",
        })
        assert r.status_code == 303 and "err" not in r.headers["location"]
        assert await db.get_state_bool("paused") and not await db.get_state_bool("approval_mode")
        assert not await db.get_state_bool("innercircle_gate", default=True)
        assert await db.get_state("delay_override") == "45" and await db.get_state("quiet_end") == "08:00"
        assert await db.get_state("voice_override") == "off"
        r = await c.post("/settings/live", data={"delay_override": "abc"})
        assert "err=" in r.headers["location"] and await db.get_state("delay_override") == "45"
        r = await c.post("/settings/live", data={"quiet_start": "23:00"})
        assert "err=" in r.headers["location"]

        # Final review I1: saving the form must only write what the user changed.
        # /quiet accepts "1:00"; the time input shows it zero-padded, and an unrelated
        # save must not wipe it. A /pause sent after the page loaded must survive too.
        await db.set_state("quiet_start", "1:00")
        r = await c.get("/settings")
        assert 'value="01:00"' in r.text and 'name="orig_quiet_start" value="01:00"' in r.text
        await db.set_state_bool("paused", True)  # /pause from the phone, after page load
        page_state = {  # what the browser submits: originals as rendered, only approval toggled
            "orig_paused": "off", "orig_approval_mode": "off", "approval_mode": "on",
            "orig_innercircle_gate": "off", "orig_voice_override": "off", "voice_override": "off",
            "orig_quiet_start": "01:00", "quiet_start": "01:00",
            "orig_quiet_end": "08:00", "quiet_end": "08:00",
            "orig_delay_override": "45", "delay_override": "45",
        }
        r = await c.post("/settings/live", data=page_state)
        assert "err" not in r.headers["location"]
        assert await db.get_state("quiet_start") == "1:00" and await db.get_state_bool("paused")
        assert await db.get_state_bool("approval_mode")
        await db.set_state_bool("approval_mode", False)

        # Final review I2: Persian digits are normalized before they reach .env (pydantic
        # rejects them at import, which would crash-loop the bot after a restart).
        r = await c.post("/settings/config", data=config_form(OWNER_USER_ID="۸۰۱", HISTORY_TURNS="۲۰"))
        assert "err" not in r.headers["location"], r.headers["location"]
        vals = dotenv_values(ENV)
        assert vals["OWNER_USER_ID"] == "801" and vals["HISTORY_TURNS"] == "20"
        r = await c.post("/settings/config", data=config_form(OWNER_USER_ID="²"))
        assert "err=" in r.headers["location"]
        r = await c.post("/settings/live", data={"delay_override": "۴۵"})
        assert await db.get_state("delay_override") == "45"

        # Config: a live field is written to .env and applied in memory without a restart.
        r = await c.post("/settings/config", data=config_form(OPENROUTER_MODEL="a/b"))
        assert r.status_code == 303 and "restart" not in r.headers["location"]
        assert dotenv_values(ENV)["OPENROUTER_MODEL"] == "a/b" and settings.openrouter_model == "a/b"
        r = await c.post("/settings/config", data=config_form(OPENROUTER_MODEL="nope/x"))
        assert "err=" in r.headers["location"] and settings.openrouter_model == "a/b"

        # Review focus 3: network down while checking a new token -> error, .env untouched.
        new_token = "654321:NEWtokenNEWtokenNEWtokenNEWtoken1"
        real = setup.tg_get_me

        def down(token):
            raise httpx.ConnectError("down")

        setup.tg_get_me = down
        r = await c.post("/settings/config", data=config_form(TG_BOT_TOKEN=new_token))
        setup.tg_get_me = real
        assert "err=" in r.headers["location"] and dotenv_values(ENV).get("TG_BOT_TOKEN") != new_token

        # A validated token change is saved and asks for a restart.
        r = await c.post("/settings/config", data=config_form(TG_BOT_TOKEN=new_token))
        assert "restart=1" in r.headers["location"] and dotenv_values(ENV)["TG_BOT_TOKEN"] == new_token
        r = await c.post("/restart")
        assert r.status_code == 200 and STOPS == [1]


async def check_contacts(app) -> None:
    async with await authed(app) as c:
        r = await c.get("/contacts")
        assert r.status_code == 200 and "/contacts/5" in r.text
        assert "/contacts/5" not in (await c.get("/contacts", params={"q": "zzz"})).text
        assert (await c.get("/contacts/5")).status_code == 200

        r = await c.post("/contacts/5/profile", data={
            "relationship": "bff", "nickname": "Sami", "persona_extra": "likes chess", "paused": "on"})
        assert r.status_code == 303 and "err" not in r.headers["location"]
        o = await db.get_override(conn_id="c1", chat_id=5)
        assert (o["relationship"], o["nickname"], o["persona_extra"], o["paused"]) == ("bff", "Sami", "likes chess", 1)
        r = await c.post("/contacts/5/profile", data={"relationship": "boss"})
        assert "err=" in r.headers["location"]

        # Review focus 2: Persian + CRLF from a textarea lands as UTF-8 with LF only.
        persian = "سلام\r\nخوبی؟"
        await c.post("/contacts/5/prompt", data={"text": persian})
        f = settings.prompts_dir / "contacts" / "5.txt"
        assert f.read_bytes() == "سلام\nخوبی؟\n".encode("utf-8")
        await c.post("/contacts/5/prompt", data={"text": "  "})
        assert not f.exists()

        await c.post("/contacts/5/memory", data={"kind": "fact", "content": "has a cat"})
        mems = await db.list_memory(conn_id="c1", chat_id=5)
        assert [m["content"] for m in mems] == ["has a cat"]
        await c.post(f"/contacts/5/memory/{mems[0]['id']}/expire")
        assert await db.list_memory(conn_id="c1", chat_id=5) == []
        r = await c.post("/contacts/5/extract")
        assert r.status_code == 303 and await db.claim_next() is not None
        assert (await c.get("/contacts/abc")).status_code == 422


async def check_prompts(app) -> None:
    personas = settings.prompts_dir / "personas"
    personas.mkdir(parents=True, exist_ok=True)
    (personas / "friend.example.txt").write_text("EXAMPLE FRIEND TEXT", encoding="utf-8")
    (settings.prompts_dir / "about_me.example.txt").write_text("ABOUT ME TEMPLATE <your name>", encoding="utf-8")
    async with await authed(app) as c:
        r = await c.get("/prompts")
        assert r.status_code == 200 and "EXAMPLE FRIEND TEXT" in r.text and "about_me" in r.text
        # Re-graded review minor: the loader never falls back to about_me.example.txt, so
        # pre-filling it would let one Save inject template text into every prompt.
        assert "ABOUT ME TEMPLATE" not in r.text
        await c.post("/prompts/gf", data={"text": "عزیزم\r\nhi"})
        assert (personas / "gf.txt").read_bytes() == "عزیزم\nhi\n".encode("utf-8")
        await c.post("/prompts/about_me", data={"text": "I live in Tehran"})
        assert (settings.prompts_dir / "about_me.txt").read_text(encoding="utf-8") == "I live in Tehran\n"
        assert (await c.post("/prompts/nope", data={"text": "x"})).status_code == 404
        assert (await c.post("/prompts/..%5Cx", data={"text": "x"})).status_code == 404
        assert not (settings.prompts_dir / "nope.txt").exists()
        assert (personas / "friend.example.txt").read_text(encoding="utf-8") == "EXAMPLE FRIEND TEXT"


async def check_drafts(app) -> None:
    sent = await db.create_pending(conn_id="c1", chat_id=5, contact_name="Sam", contact_msg="hi", draft="hey there")
    edited = await db.create_pending(conn_id="c1", chat_id=5, contact_name="Sam", contact_msg="hi", draft="draft")
    skipped = await db.create_pending(conn_id="c1", chat_id=5, contact_name="Sam", contact_msg="hi", draft="nah")
    async with await authed(app) as c:
        r = await c.get("/drafts")
        assert r.status_code == 200 and "hey there" in r.text
        r = await c.post(f"/drafts/{sent}", data={"action": "send", "text": "hey there"})
        assert "msg=" in r.headers["location"] and BOT.sent[-1] == (5, "hey there", "c1")
        assert (await db.get_pending(sent))["status"] == "approved"
        r = await c.post(f"/drafts/{sent}", data={"action": "send", "text": "hey there"})
        assert "err=" in r.headers["location"] and len(BOT.sent) == 1  # double submit can't resend
        await c.post(f"/drafts/{edited}", data={"action": "send", "text": "my own words\r\n"})
        assert BOT.sent[-1] == (5, "my own words", "c1") and (await db.get_pending(edited))["status"] == "edited"
        await c.post(f"/drafts/{skipped}", data={"action": "skip"})
        assert (await db.get_pending(skipped))["status"] == "skipped" and len(BOT.sent) == 2


async def check_hosted() -> None:
    old = (settings.hosted, settings.dashboard_proxy_secret)
    settings.hosted, settings.dashboard_proxy_secret = True, "s3cret-proxy"
    try:
        app = create_app(BOT, lambda: STOPS.append(1), env_path=ENV)
        # No header: refused, and no redirect to the login page.
        async with client(app) as c:
            r = await c.get("/settings")
            assert r.status_code == 403, r.status_code
            r = await c.get("/settings", headers={"x-platform-auth": "wrong"})
            assert r.status_code == 403
        hdr = {"x-platform-auth": "s3cret-proxy"}
        # Header works even with a non-loopback Host (the proxy may forward any Host).
        async with client(app, base="http://example.com", headers=hdr) as c:
            r = await c.get("/settings")
            assert r.status_code == 200, r.status_code
            assert "TG_BOT_TOKEN" not in r.text and "OPENROUTER_API_KEY" not in r.text
            assert "OWNER_USER_ID" not in r.text
            assert 'action="/logout"' not in r.text
            # Saving without the hidden fields works and never touches them.
            r = await c.post("/settings/config", data={
                "OPENROUTER_MODEL": "a/b", "EXTRACTOR_MODEL": "google/gemini-3.1-flash-lite",
                "WHISPER_MODEL": "openai/whisper-large-v3", "OWNER_FIRST_NAME": "Sep",
                "HISTORY_TURNS": "12",
                "TG_BOT_TOKEN": "999:evil", "OWNER_USER_ID": "666",
            })
            assert r.status_code == 303, r.status_code
            assert "err=" not in r.headers["location"], r.headers["location"]
            env_text = ENV.read_text(encoding="utf-8")
            assert "999:evil" not in env_text and "666" not in env_text
    finally:
        settings.hosted, settings.dashboard_proxy_secret = old


async def check_proxy_ok_empty_secret() -> None:
    old = settings.dashboard_proxy_secret
    settings.dashboard_proxy_secret = ""
    try:
        assert not auth.proxy_ok("")      # empty secret never authenticates
        assert not auth.proxy_ok(None)
    finally:
        settings.dashboard_proxy_secret = old


async def check_root_path() -> None:
    old = (settings.hosted, settings.dashboard_proxy_secret, settings.dashboard_root_path)
    settings.hosted, settings.dashboard_proxy_secret, settings.dashboard_root_path = True, "p", "/app"
    try:
        app = create_app(BOT, lambda: STOPS.append(1), env_path=ENV)
        async with client(app, headers={"x-platform-auth": "p"}) as c:
            r = await c.get("/")
            assert r.status_code == 200
            for bad in ('href="/"', 'href="/settings"', 'href="/static/'):
                assert bad not in r.text, bad
            assert 'href="/app/settings"' in r.text and 'href="/app/static/style.css"' in r.text
            r = await c.post("/settings/config", data={"HISTORY_TURNS": ""})
            assert r.headers["location"].startswith("/app/settings"), r.headers["location"]
    finally:
        settings.hosted, settings.dashboard_proxy_secret, settings.dashboard_root_path = old


async def check_persian() -> None:
    import secretary.dashboard.web as web
    old = (settings.hosted, settings.dashboard_proxy_secret, settings.dashboard_lang)
    settings.hosted, settings.dashboard_proxy_secret, settings.dashboard_lang = True, "p", "fa"
    try:
        app = create_app(BOT, lambda: STOPS.append(1), env_path=ENV)
        async with client(app, headers={"x-platform-auth": "p"}) as c:
            for path in ("/", "/settings", "/contacts", "/contacts/5", "/prompts", "/drafts"):
                r = await c.get(path)
                assert r.status_code == 200, (path, r.status_code)
                assert 'dir="rtl"' in r.text and 'lang="fa"' in r.text, path
                assert "تنظیمات" in r.text, path          # nav: Settings
                for english in (">Settings<", ">Contacts<", ">Drafts<", ">Save<", ">Status<", "Save changes",
                                "Advanced settings", "Wait before replying"):
                    assert english not in r.text, (path, english)
            r = await c.get("/settings")
            assert "تنظیمات پیشرفته" in r.text and "TG_BOT_TOKEN" not in r.text  # hosted: no token field
            r = await c.post("/settings/config", data={"HISTORY_TURNS": ""})
            assert "err=" in r.headers["location"] and "%D9" in r.headers["location"]  # Persian flash
        assert web.t("never-translated-xyz") == "never-translated-xyz"
    finally:
        settings.hosted, settings.dashboard_proxy_secret, settings.dashboard_lang = old
    assert web.t("Settings") == "Settings"  # default en: untouched


async def check_credit_notice() -> None:
    from secretary import handlers
    await db.set_state("credit_notice_at", "0")
    assert await handlers._credit_notice_due() is True
    assert await handlers._credit_notice_due() is False      # within the hour
    await db.set_state("credit_notice_at", str(int(time.time()) - 3601))
    assert await handlers._credit_notice_due() is True


async def check_update_intake() -> None:
    import asyncio as _a
    from telegram import Update
    q: _a.Queue = _a.Queue()
    upd = {"update_id": 77, "message": {"message_id": 1, "date": 0,
           "chat": {"id": 111, "type": "private"}, "from": {"id": 111, "is_bot": False, "first_name": "O"},
           "text": "/pause"}}
    old = (settings.hosted, settings.dashboard_proxy_secret)
    settings.hosted, settings.dashboard_proxy_secret = True, "p"
    try:
        app = create_app(BOT, lambda: None, env_path=ENV, update_queue=q)
        async with client(app) as c:
            assert (await c.post("/_tg/update", json=upd)).status_code == 403
            r = await c.post("/_tg/update", json=upd, headers={"x-platform-auth": "p"})  # proxy-style: no route header
            assert r.status_code in (403, 404) and q.empty(), r.status_code
            r = await c.post("/_tg/update", json=upd,
                             headers={"x-platform-auth": "p", "x-platform-route": "update"})
            assert r.status_code == 200, r.status_code
        got = q.get_nowait()
        assert isinstance(got, Update) and got.update_id == 77 and got.message.text == "/pause"
    finally:
        settings.hosted, settings.dashboard_proxy_secret = old
    # Not hosted: the endpoint does not exist.
    app = create_app(BOT, lambda: None, env_path=ENV, update_queue=q)
    async with client(app) as c:
        r = await c.post("/_tg/update", json=upd, headers=ORIGIN)
        assert r.status_code in (401, 404), r.status_code


async def check_hosted_connection() -> None:
    from types import SimpleNamespace as NS
    from secretary import handlers
    calls: list[str] = []

    def ctx(uid: int):
        async def gbc(cid):
            calls.append(cid)
            return NS(id=cid, user=NS(id=uid), user_chat_id=uid, is_enabled=True,
                      rights=NS(can_reply=True, can_read_messages=True, to_dict=lambda: {"can_reply": True}))
        return NS(bot=NS(get_business_connection=gbc))

    old = (settings.hosted, settings.owner_user_id)
    try:
        settings.hosted, settings.owner_user_id = False, 5150
        assert await handlers._connection(ctx(5150), "hc0") is None and calls == []   # local: no Telegram call
        settings.hosted = True
        assert await handlers._connection(ctx(999), "hc1") is None                    # stranger: nothing stored
        assert await db.get_connection("hc1") is None
        row = await handlers._connection(ctx(5150), "hc2")                            # owner: stored
        assert row and row["owner_chat_id"] == 5150 and await db.get_connection("hc2")
        n = len(calls)
        assert await handlers._connection(ctx(5150), "hc2") and len(calls) == n       # cached after first fetch

        # Connected without the reply right: the owner DM explains the fix, in Persian when fa.
        sent: list[str] = []

        async def send_message(chat_id, text, **_):
            sent.append(text)

        bc = NS(id="hc3", user=NS(id=5150), user_chat_id=5150, is_enabled=True,
                rights=NS(can_reply=False, can_read_messages=True, to_dict=lambda: {}))
        bot_ctx = NS(bot=NS(username="sec_bot", send_message=send_message))
        await handlers.on_business_connection(NS(business_connection=bc), bot_ctx)
        assert "Missing rights: reply" in sent[-1], sent
        settings.dashboard_lang = "fa"
        await handlers.on_business_connection(NS(business_connection=bc), bot_ctx)
        assert "Chat Automation > @sec_bot" in sent[-1] and "روشن کنید" in sent[-1], sent
    finally:
        settings.hosted, settings.owner_user_id = old
        settings.dashboard_lang = "en"


async def main() -> None:
    await db.init_db()
    try:
        await check_db()
        app = create_app(BOT, lambda: STOPS.append(1), env_path=ENV)
        await check_auth(app)
        await check_port_busy()
        await check_status(app)
        await check_settings(app)
        await check_contacts(app)
        await check_prompts(app)
        await check_drafts(app)
        await check_hosted()
        await check_proxy_ok_empty_secret()
        await check_root_path()
        await check_persian()
        await check_credit_notice()
        await check_update_intake()
        await check_hosted_connection()
    finally:
        await db.close_db()  # an open aiosqlite thread would hang the process on failure
    print("smoke_dashboard OK")


asyncio.run(main())
