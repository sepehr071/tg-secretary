"""Memory extraction + rendering + style-fingerprint pipeline."""
import asyncio
import json
import logging
import time

from openai import AsyncOpenAI

from . import db
from .config import settings

log = logging.getLogger(__name__)

# Reuse OpenRouter via OpenAI SDK — separate client lets us hit a different model.
_extractor = AsyncOpenAI(
    api_key=settings.openrouter_api_key,
    base_url="https://openrouter.ai/api/v1",
    default_headers={
        "HTTP-Referer": "https://github.com/sepehr071/secretary-bot",
        "X-Title": "Personal Secretary Bot (extractor)",
    },
)

EXTRACTION_SYSTEM = """You extract DURABLE, NOTEWORTHY facts about the CONTACT (not the owner) from a recent slice of conversation.

Return STRICT JSON, no prose, no markdown fences:
{"memories": [{"kind": "fact|promise|event|preference|inside_joke|open_thread", "content": "<one short sentence>", "expires_at": null | <unix_ts>}]}

Hard rules (violation = unusable output):
1. Subject must be the CONTACT (the human messaging the owner), never "the user" / "the assistant" / "the bot". Use the contact's real name if visible; otherwise write "She" / "He" / "They".
2. SKIP if the slice contains nothing new and durable. An empty {"memories": []} is GOOD output — do not fabricate.
3. Re-read the "Existing memory" block. If a candidate memory is already there (even paraphrased), DO NOT emit it again. Dedup aggressively.
4. Do NOT extract: greetings, individual emoji, single-word reactions, swearing, sexual talk, casual jokes, moods of the moment, sentences fragments. Only durable signal.
5. "fact" = permanent reality (job, family, city, pet, birthday).
6. "preference" = stable communication preference (language, message length, emoji policy). Not one-off requests.
7. "promise" = a real commitment with a fulfilment criterion.
8. "event" = a real scheduled occurrence (interview Tuesday, trip next month). expires_at = unix timestamp of event + 7 days.
9. "inside_joke" = a recurring callback that appears MULTIPLE times across the slice.
10. "open_thread" = a real unresolved topic the contact cares about. Not "the user uses crude language" — that's noise.
11. Be conservative. When in doubt, return fewer items.
12. Maximum 5 memories per call. Quality over quantity. Returning 0 is fine.
"""

STYLE_SYSTEM = """Analyze how the OWNER writes to this contact. Return STRICT JSON:
{"avg_length": <int>, "formality": "casual|warm|formal", "emoji_freq": "none|light|heavy", "languages": ["en","fa",...], "pet_names": [...], "signature_open": "<string or empty>", "signature_close": "<string or empty>"}

Do NOT capture "recurring phrases" — that field used to create a feedback loop where the bot's own outputs got fed back as instructions. Stick to durable owner-voice signals (length, formality, language mix, pet names, signature openers/closers).

No prose, no markdown fences.
"""


async def render_memory_block(conn_id: str, chat_id: int) -> str | None:
    """Format active memory rows into a compact block for system-prompt injection."""
    rows = await db.list_memory(conn_id=conn_id, chat_id=chat_id)
    if not rows:
        return None
    # priority order
    order = {"fact": 0, "preference": 1, "inside_joke": 2, "promise": 3, "open_thread": 4, "event": 5}
    rows.sort(key=lambda r: (order.get(r["kind"], 99), -r["created_at"]))
    # Gemini 3 Flash has a 1M context — be generous, the main reply model can absorb it.
    lines = [f"- [{r['kind']}] {r['content']}" for r in rows[:200]]
    return "\n".join(lines)


async def enqueue_if_due(conn_id: str, chat_id: int) -> None:
    """Throttled enqueue — db.enqueue rejects if recent pending exists."""
    await db.enqueue(conn_id=conn_id, chat_id=chat_id)


MIN_NEW_MESSAGES_FOR_EXTRACTION = 10


async def _extract_for_chat(conn_id: str, chat_id: int) -> None:
    """Extract durable memories from messages not yet processed.
    Skips if fewer than MIN_NEW_MESSAGES_FOR_EXTRACTION new messages.
    Dedups against existing memory before inserting.
    Marks processed messages with extracted_at=now so they aren't reprocessed.
    """
    new_rows = await db.list_unextracted_messages(
        conn_id=conn_id, chat_id=chat_id, limit=300
    )
    if len(new_rows) < MIN_NEW_MESSAGES_FOR_EXTRACTION:
        log.debug(
            "extract skipped chat=%s: only %d unextracted messages (need %d)",
            chat_id, len(new_rows), MIN_NEW_MESSAGES_FOR_EXTRACTION,
        )
        return

    existing = await db.list_memory(conn_id=conn_id, chat_id=chat_id)
    existing_text = "\n".join(f"- [{m['kind']}] {m['content']}" for m in existing[:300])
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in new_rows)
    prompt = (
        f"Existing memory (do NOT re-emit any of these, even paraphrased):\n"
        f"{existing_text or '(none)'}\n\n"
        f"NEW conversation slice (only this window — older context already captured above):\n"
        f"{convo}\n\n"
        "Return JSON. Empty memories array is the correct answer if nothing durable + new is here."
    )

    try:
        resp = await _extractor.chat.completions.create(
            model=settings.extractor_model,
            messages=[
                {"role": "system", "content": EXTRACTION_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=2000,
            response_format={"type": "json_object"},
        )
        raw = (resp.choices[0].message.content or "").strip()
        parsed = json.loads(raw)
        added = 0
        for m in parsed.get("memories", [])[:5]:
            kind = m.get("kind")
            content = m.get("content")
            expires = m.get("expires_at")
            if not kind or not content:
                continue
            # Insert-side dedup: skip if a near-duplicate memory already exists.
            if await db.memory_duplicate_exists(
                conn_id=conn_id, chat_id=chat_id, kind=kind, content=content
            ):
                log.debug("memory dedup: skipped %s/%r", kind, content[:60])
                continue
            await db.add_memory(
                conn_id=conn_id, chat_id=chat_id,
                kind=kind, content=content,
                expires_at=expires if isinstance(expires, int) else None,
            )
            added += 1
        log.info(
            "extract chat=%s processed=%d added=%d skipped_dup=%d",
            chat_id, len(new_rows), added,
            len(parsed.get("memories", [])) - added,
        )
    except Exception:
        log.exception("memory extraction failed for chat %s", chat_id)
        return  # don't mark messages extracted if we failed — try again next round

    # Mark these messages as processed so they don't re-feed the extractor.
    await db.mark_messages_extracted([r["id"] for r in new_rows])


async def extract_worker() -> None:
    """Background loop. Drains extraction_queue.
    After each job, opportunistically tries summary + style refresh for that chat —
    both are no-ops if their internal staleness checks reject (so cheap to call).
    Runs off the user-facing hot path, so cost/latency only hits the extractor model.
    """
    log.info("memory extractor worker started")
    while True:
        try:
            job = await db.claim_next()
            if job is None:
                await asyncio.sleep(15)
                continue
            conn_id, chat_id = job["conn_id"], job["chat_id"]
            await _extract_for_chat(conn_id, chat_id)
            # Opportunistic follow-ups — each has its own staleness gate.
            try:
                await summarize_old_history(conn_id, chat_id)
            except Exception:
                log.exception("summarize_old_history failed in worker")
            try:
                await maybe_refresh_style(conn_id, chat_id)
            except Exception:
                log.exception("maybe_refresh_style failed in worker")
            await db.mark_processed(job["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("extractor loop error")
            await asyncio.sleep(5)


async def maybe_refresh_style(conn_id: str, chat_id: int) -> None:
    """Refresh style fingerprint when owner has written 30+ messages and it's stale.
    Pulls a deep window of history (1M-context model can absorb it).
    """
    history = await db.load_history(conn_id=conn_id, chat_id=chat_id, limit=1000)
    owner_msgs = [m for m in history if m["role"] == "assistant"]
    if len(owner_msgs) < 30:
        return
    override = await db.get_override(conn_id=conn_id, chat_id=chat_id)
    if override and override.get("style_updated_at"):
        age = int(time.time()) - int(override["style_updated_at"])
        if age < 30 * 86400:
            return
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in history)
    try:
        resp = await _extractor.chat.completions.create(
            model=settings.extractor_model,
            messages=[
                {"role": "system", "content": STYLE_SYSTEM},
                {"role": "user", "content": convo},
            ],
            temperature=0.2,
            max_tokens=2000,
            response_format={"type": "json_object"},
        )
        raw = (resp.choices[0].message.content or "").strip()
        json.loads(raw)  # validate
        await db.set_style_fingerprint(conn_id=conn_id, chat_id=chat_id, json_str=raw)
    except Exception:
        log.exception("style fingerprint refresh failed")


async def summarize_old_history(conn_id: str, chat_id: int) -> None:
    """Roll up older messages into chat_summaries when history > threshold.
    With a 1M-context reply model we keep the summary rich (~1k tokens) so the
    main LLM has narrative context for months-old threads without burning raw turns.
    Throttled to once per chat per 6h to avoid re-summarizing every job.
    """
    history = await db.load_history(conn_id=conn_id, chat_id=chat_id, limit=2000)
    if len(history) < settings.history_turns * 4:
        return
    # Throttle: skip if a summary exists newer than 6 hours.
    existing = await db.get_summary(conn_id=conn_id, chat_id=chat_id)
    if existing and int(time.time()) - int(existing.get("updated_at", 0)) < 6 * 3600:
        return
    older = history[:-settings.history_turns * 2]
    if not older:
        return
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in older)
    try:
        resp = await _extractor.chat.completions.create(
            model=settings.extractor_model,
            messages=[
                {"role": "system", "content": (
                    "Summarize this conversation in up to ~1000 tokens. "
                    "Preserve: names, relationships, ongoing threads, recent "
                    "events, promises, mood arcs, recurring topics, inside "
                    "references, what they last talked about. Write narrative "
                    "prose, not bullet points. No translations, no markdown."
                )},
                {"role": "user", "content": convo},
            ],
            temperature=0.2,
            max_tokens=4000,
        )
        summary = (resp.choices[0].message.content or "").strip()
        if summary:
            await db.upsert_summary(
                conn_id=conn_id, chat_id=chat_id,
                summary=summary,
                summarized_up_to_msg_id=0,
            )
    except Exception:
        log.exception("summarization failed for chat %s", chat_id)
