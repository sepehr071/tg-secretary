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
2. Content must be a self-contained sentence the bot can later READ and ACT on. Bad: "she mentioned a movie". Good: "She loves the Batman films, especially the Nolan trilogy."
3. SKIP if the slice contains nothing new and durable. An empty {"memories": []} is GOOD output — do not fabricate.
4. Re-read the "Existing memory" block. If a candidate memory is already there (even paraphrased), DO NOT emit it again. Dedup aggressively.
5. Do NOT extract: greetings, individual emoji, single-word reactions, swearing, sexual talk, casual jokes, moods of the moment, sentence fragments. Only durable signal.
6. "fact" = permanent reality (job, family, city, pet, birthday, hobbies, tastes, fandom, dietary habits). FAVOR these — they're the most useful for sounding like you know them.
7. "preference" = stable communication preference (language, message length, emoji policy, pet names they like/hate). Not one-off requests.
8. "promise" = a real commitment with a fulfilment criterion.
9. "event" = a real scheduled occurrence (interview Tuesday, trip next month). expires_at = unix timestamp of event + 7 days.
10. "inside_joke" = a recurring callback that appears MULTIPLE times across the slice.
11. "open_thread" = a real unresolved topic the contact cares about and might bring up again. Not "the user uses crude language" — that's noise.
12. Don't be precious. If you see ANY durable signal, capture it. Worse to forget than to over-capture (dedup runs after you).
13. Maximum 6 memories per call.
"""

PROFILE_SYSTEM = """You write a 200-400 word narrative profile of a specific person ({owner_first_name} talks to). The profile will be injected into the system prompt of a chatbot impersonating {owner_first_name}, so the bot reads it before every reply and uses it to sound like someone who actually knows this person.

You are given:
- Atomic facts already extracted about them (job, life context, preferences, in-jokes, open threads, events).
- An optional conversation summary covering older history.

Write a flowing prose paragraph (or two) that covers, in your own words and woven together:
- Who they are: name, job/life-stage, where they live, key people in their life.
- How they relate to {owner_first_name}: history together, recurring dynamics, in-jokes, level of intimacy.
- Their personality and communication style: formality, humor, what they care about, what bores them.
- Open threads and recurring topics they're likely to bring up.
- Sensitive ground: topics to handle gently, things to never bring up unprompted.

Hard rules:
- Narrative prose. Not a list. Not headers. Not "1." / "•" / "-". One reader-friendly paragraph (or two short ones).
- Stay grounded in the supplied facts. Don't invent details. If something is unknown, omit it. Better to be shorter than to fabricate.
- Use the contact's real name when known; otherwise "she" / "he" / "they".
- Write as a third-person briefing TO {owner_first_name}'s bot, not as the contact and not as {owner_first_name}.
- Plain text only. No markdown. No greetings. No "Here's the profile:" preamble. Just the paragraph(s).
- 200-400 words. Cut anything that isn't actionable for sounding like you know them.
"""

STYLE_SYSTEM = """Analyze how the OWNER writes to this contact. Return STRICT JSON:
{"avg_length": <int>, "formality": "casual|warm|formal", "emoji_freq": "none|light|heavy", "languages": ["en","fa",...], "pet_names": [...], "signature_open": "<string or empty>", "signature_close": "<string or empty>"}

Do NOT capture "recurring phrases" — that field used to create a feedback loop where the bot's own outputs got fed back as instructions. Stick to durable owner-voice signals (length, formality, language mix, pet names, signature openers/closers).

No prose, no markdown fences.
"""


_KIND_HEADERS: tuple[tuple[str, str], ...] = (
    ("fact", "Facts about them"),
    ("preference", "How they like to be talked to"),
    ("inside_joke", "Inside jokes / recurring callbacks"),
    ("open_thread", "Open threads (unresolved topics they care about — follow up if relevant)"),
    ("promise", "Promises made to them (don't forget)"),
    ("event", "Upcoming / recent events"),
)


async def render_memory_block(conn_id: str, chat_id: int) -> str | None:
    """Format active memory rows into a system-prompt block, grouped by kind.
    Grouping helps the reply model treat facts as facts, threads as threads, etc.
    """
    rows = await db.list_memory(conn_id=conn_id, chat_id=chat_id)
    if not rows:
        return None
    by_kind: dict[str, list[dict]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    # Newest first inside each bucket so recent context surfaces first.
    for bucket in by_kind.values():
        bucket.sort(key=lambda r: -r["created_at"])
    sections: list[str] = []
    for kind, header in _KIND_HEADERS:
        bucket = by_kind.pop(kind, None)
        if not bucket:
            continue
        lines = [f"- {r['content']}" for r in bucket[:50]]
        sections.append(f"### {header}\n" + "\n".join(lines))
    # Any unknown kind (forward-compat) goes at the bottom under its raw name.
    for kind, bucket in by_kind.items():
        lines = [f"- {r['content']}" for r in bucket[:50]]
        sections.append(f"### {kind.title()}\n" + "\n".join(lines))
    return "\n\n".join(sections) if sections else None


async def enqueue_if_due(conn_id: str, chat_id: int) -> None:
    """Throttled enqueue — db.enqueue rejects if recent pending exists."""
    await db.enqueue(conn_id=conn_id, chat_id=chat_id)


MIN_NEW_MESSAGES_FOR_EXTRACTION = 5


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
        for m in parsed.get("memories", [])[:6]:
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
    After each job, opportunistically tries summary + style + profile refresh —
    each has its own staleness gate (cheap to call, no-op if not due).
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
            try:
                await maybe_refresh_profile(conn_id, chat_id)
            except Exception:
                log.exception("maybe_refresh_profile failed in worker")
            await db.mark_processed(job["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("extractor loop error")
            await asyncio.sleep(5)


# Profile refresh thresholds.
PROFILE_MIN_MEMORIES = 3              # need at least this many facts to synthesize
PROFILE_STALE_SECONDS = 7 * 86400     # 7 days
PROFILE_NEW_MEMORY_TRIGGER = 10       # refresh if this many new memory rows since last build


async def maybe_refresh_profile(
    conn_id: str, chat_id: int, *, force: bool = False
) -> str | None:
    """Lazy refresh of the per-contact narrative profile.

    Skip unless:
      - force=True (owner ran /profile <id> refresh), OR
      - no profile yet AND >= PROFILE_MIN_MEMORIES active memory rows, OR
      - existing profile is older than PROFILE_STALE_SECONDS, OR
      - >= PROFILE_NEW_MEMORY_TRIGGER new memory rows since last build.

    Returns the new profile text on refresh, None if skipped/failed.
    """
    memories = await db.list_memory(conn_id=conn_id, chat_id=chat_id)
    if not force and len(memories) < PROFILE_MIN_MEMORIES:
        return None

    override = await db.get_override(conn_id=conn_id, chat_id=chat_id)
    existing_profile = override.get("profile") if override else None
    last_update = (override.get("profile_updated_at") if override else None) or 0
    last_count = (override.get("profile_memory_count_at_update") if override else None) or 0

    if not force and existing_profile:
        age = int(time.time()) - int(last_update)
        new_memories = len(memories) - int(last_count)
        if age < PROFILE_STALE_SECONDS and new_memories < PROFILE_NEW_MEMORY_TRIGGER:
            return None

    if not memories and not force:
        return None

    # Build the input: memory rows grouped + chat summary (if any).
    fact_lines = [f"- [{m['kind']}] {m['content']}" for m in memories]
    summary_row = await db.get_summary(conn_id=conn_id, chat_id=chat_id)
    summary_text = summary_row["summary"] if summary_row else ""
    user_payload = (
        "Atomic facts already extracted about this person:\n"
        + ("\n".join(fact_lines) if fact_lines else "(none)")
        + "\n\nOlder conversation summary:\n"
        + (summary_text or "(none)")
        + "\n\nWrite the profile."
    )
    system_prompt = PROFILE_SYSTEM.replace(
        "{owner_first_name}", settings.owner_first_name
    )

    try:
        resp = await _extractor.chat.completions.create(
            model=settings.extractor_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_payload},
            ],
            temperature=0.3,
            max_tokens=1500,
        )
        profile_text = (resp.choices[0].message.content or "").strip()
    except Exception:
        log.exception("profile refresh LLM call failed (chat=%s)", chat_id)
        return None

    if not profile_text:
        log.warning("profile refresh produced empty output (chat=%s)", chat_id)
        return None

    await db.set_profile(
        conn_id=conn_id, chat_id=chat_id,
        profile=profile_text, memory_count=len(memories),
    )
    log.info(
        "profile refreshed chat=%s memories=%d chars=%d force=%s",
        chat_id, len(memories), len(profile_text), force,
    )
    return profile_text


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
