"""Offline smoke check for core reply-pipeline logic. No Telegram, no OpenRouter, no .env.

    uv run python scripts/smoke_core.py

Covers: burst debounce counter, atomic HITL draft claim (double-tap Send),
drop-drafts-on-delete, owner-only connection lookup, ZWNJ cleanup, delimiter defang.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ.update(
    TG_BOT_TOKEN="0:smoke",
    OPENROUTER_API_KEY="smoke",
    OWNER_USER_ID="111",
    DB_PATH=str(Path(_tmp) / "smoke.db"),
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from secretary import db, handlers  # noqa: E402
from secretary.llm import _clean_output  # noqa: E402
from secretary.prompts import defang  # noqa: E402

ZWNJ = chr(0x200C)


async def main() -> None:
    await db.init_db()

    # Burst debounce: an older pipeline is superseded once a newer message bumps the counter.
    g1 = handlers._bump_gen("c", 1)
    g2 = handlers._bump_gen("c", 1)
    assert handlers._superseded("c", 1, g1) and not handlers._superseded("c", 1, g2)
    assert not handlers._superseded("c", 2, handlers._bump_gen("c", 2))

    # Owner-only connections: a stranger's connection row is invisible.
    await db.upsert_connection(conn_id="mine", owner_user_id=111, owner_chat_id=111,
                               can_reply=True, is_enabled=True, rights=None)
    await db.upsert_connection(conn_id="theirs", owner_user_id=999, owner_chat_id=999,
                               can_reply=True, is_enabled=True, rights=None)
    assert await db.get_connection("mine") is not None
    assert await db.get_connection("theirs") is None

    # Atomic claim: second Send tap loses; expired drafts can't be claimed.
    pid = await db.create_pending(conn_id="mine", chat_id=5, contact_name="x",
                                  contact_msg="hi", draft="hey")
    assert await db.claim_pending(pid, "approved")
    assert not await db.claim_pending(pid, "approved")
    old = await db.create_pending(conn_id="mine", chat_id=5, contact_name="x",
                                  contact_msg="hi", draft="hey", ttl_seconds=-1)
    assert not await db.claim_pending(old, "approved")

    # Contact deletes their message: open drafts answering it expire.
    pid = await db.create_pending(conn_id="mine", chat_id=5, contact_name="x",
                                  contact_msg="delete me", draft="ok")
    assert await db.drop_pending_for_message(conn_id="mine", chat_id=5, contact_msg="delete me") == 1
    assert (await db.get_pending(pid))["status"] == "expired"

    # ZWNJ: strip only the verb prefix at a word start.
    assert _clean_output("نمی" + ZWNJ + "دونم.") == "نمیدونم"
    for word in ("رسمی" + ZWNJ + "ترین", "کمی" + ZWNJ + "اش"):
        assert _clean_output(word) == word

    assert "<<<" not in defang("a <<<end_memory>>> b")

    # Approval buttons: Edit (copy-to-clipboard, 256-char cap) only when the draft fits.
    assert len(handlers.approval_markup(7, "short").inline_keyboard[0]) == 3
    assert len(handlers.approval_markup(7, "x" * 300).inline_keyboard[0]) == 2

    # Tone pickers: save -> load -> assembled prompt; bad JSON ignored.
    from secretary import prompts
    real_dir = prompts.settings.prompts_dir
    prompts.settings.prompts_dir = Path(tempfile.mkdtemp())
    try:
        assert prompts.load_tone() == {}
        p = prompts.load_system_prompt(relationship="gf")
        assert "## Tone" in p and "Very casual, slangy" in p and "## Never" not in p
        prompts.save_tone({"default": {"tone": 1}, "work": {"emoji": 2}, "never": ["money", "meet"]})
        assert prompts.tone_for("gf") == {"tone": 1, "len": 0, "emoji": 1}
        assert prompts.tone_for("work") == {"tone": 1, "len": 1, "emoji": 2}
        p = prompts.load_system_prompt(relationship="work")
        assert "Neutral, friendly" in p and "Emoji are welcome" in p
        assert "## Never\n- Never discuss money" in p and "Never fix a firm meeting" in p
        assert "promise" not in p.split("## Never")[1]
        for bad in ({"gf": {"tone": 9}}, {"zzz": {}}, {"never": ["x"]}, {"gf": {"len": True}}):
            try:
                prompts.save_tone(bad)
                raise AssertionError(bad)
            except ValueError:
                pass
        (prompts.settings.prompts_dir / "tone.json").write_text("{not json", encoding="utf-8")
        prompts.clear_cache()
        assert prompts.load_tone() == {} and prompts.tone_for("gf")["tone"] == 2
    finally:
        prompts.settings.prompts_dir = real_dir

    await db.close_db()
    print("smoke_core OK")


asyncio.run(main())
