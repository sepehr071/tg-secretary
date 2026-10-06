"""Offline checks for the hosting control plane. No Telegram, OpenRouter or pm2.

    PYTHONIOENCODING=utf-8 uv run python scripts/smoke_hosting.py
"""
import asyncio
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

from hosting import db  # noqa: E402
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
