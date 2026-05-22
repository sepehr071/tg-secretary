"""Anonymized fixture dump from a real secretary.db into tests/fixtures/scenarios/.

Reads chats from a source SQLite database, anonymizes names / phones / emails / URLs
deterministically (so the same chat anonymizes the same way across runs), and writes
one scenario JSON per chat. Run this against your own `./secretary.db` to grow the
test suite with realistic fixtures.

PII handling:
- Names: replaced from a built-in alias pool, keyed by sha256(chat_id) so the same
  chat always maps to the same alias. Both Telegram profile fields and any occurrence
  of the real name inside persona_extra / memory content / message content are swapped.
- Phone numbers (Iranian +98, local 0912, generic): redacted to +98XXXXXXXXXX.
- Emails: user@example.com.
- URLs: https://example.com/PATH.
- Emojis, swears, code-switching, register, locations — preserved.

Alias map is printed to stderr at the end (never written to disk) so you can sanity-check
that names you recognise got swapped.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent

# Built-in alias pools — kept small + obviously fake so eyeballing a fixture is fast.
FA_ALIASES = [
    "آرش", "بهنام", "پرهام", "تینا", "ثمین", "جاوید", "چیستا", "حامد",
    "خاطره", "دارا", "ذاکر", "رها", "زهرا", "سارا", "شیما", "صدف",
    "ضحی", "طاها", "ظریف", "عرفان", "غزل", "فرید", "قاسم", "کیان",
    "گلسا", "لاله", "میلاد", "نگار", "هانیه", "یاسمن",
]
EN_ALIASES = [
    "Alex", "Blair", "Cameron", "Dana", "Ellis", "Finley", "Gray", "Harper",
    "Ira", "Jordan", "Kai", "Logan", "Morgan", "Nico", "Oakley", "Parker",
    "Quinn", "Riley", "Sage", "Taylor", "Umi", "Val", "Wren", "Xen",
    "Yael", "Zion", "Avery", "Bryn", "Cary", "Devon",
]

PHONE_RX = re.compile(r"(?:\+?98|0)\s?9\d{2}[\s\-]?\d{3}[\s\-]?\d{4}")
GENERIC_PHONE_RX = re.compile(r"\+?\d{10,15}")
EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
URL_RX = re.compile(r"https?://[^\s]+")


def _alias_for(chat_id: int, kind: str, original: str | None) -> str | None:
    """Deterministic alias from (chat_id, kind, original). Returns None if original is None/empty."""
    if not original:
        return None
    seed = f"{chat_id}|{kind}|{original}".encode()
    h = int(hashlib.sha256(seed).hexdigest()[:8], 16)
    # If original contains any Latin letters, use English pool; otherwise Farsi.
    pool = EN_ALIASES if any("a" <= c.lower() <= "z" for c in original) else FA_ALIASES
    return pool[h % len(pool)]


def _redact(text: str | None) -> str | None:
    if not text:
        return text
    text = URL_RX.sub("https://example.com/REDACTED", text)
    text = EMAIL_RX.sub("user@example.com", text)
    text = PHONE_RX.sub("+98XXXXXXXXXX", text)
    text = GENERIC_PHONE_RX.sub("+98XXXXXXXXXX", text)
    return text


def _swap_names(text: str | None, swaps: dict[str, str]) -> str | None:
    """Case-sensitive whole-word replacement of every real name with its alias."""
    if not text or not swaps:
        return text
    # Sort longest-first so "Sara Smith" matches before "Sara".
    for original in sorted(swaps.keys(), key=len, reverse=True):
        alias = swaps[original]
        # Replace as bare token; Farsi has no word boundaries the way \b expects,
        # so use plain str.replace which is fine for the limited alias set here.
        text = text.replace(original, alias)
    return text


def _build_swaps(chat_id: int, first: str | None, last: str | None, nick: str | None) -> dict[str, str]:
    swaps: dict[str, str] = {}
    for kind, value in (("first", first), ("last", last), ("nick", nick)):
        if value and value.strip():
            alias = _alias_for(chat_id, kind, value.strip())
            if alias:
                swaps[value.strip()] = alias
    return swaps


def _scenario_name(relationship: str, chat_id: int) -> str:
    short = hashlib.sha256(str(chat_id).encode()).hexdigest()[:8]
    return f"{relationship}_{short}"


def dump(source_db: Path, out_dir: Path, limit_per_relationship: int, owner_first_name: str) -> dict[str, str]:
    """Synchronous SQLite read (we don't need aiosqlite for a one-shot script).
    Returns the full alias map used (chat_id|kind|original -> alias) for stderr summary.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(source_db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Pull all overrides with a known relationship.
    cur.execute(
        "SELECT * FROM contact_overrides WHERE relationship IS NOT NULL AND relationship != ''"
    )
    overrides = [dict(r) for r in cur.fetchall()]

    counts: dict[str, int] = {}
    written = 0
    alias_summary: dict[str, str] = {}

    for ov in overrides:
        rel = (ov.get("relationship") or "unknown").strip().lower()
        if counts.get(rel, 0) >= limit_per_relationship:
            continue
        chat_id = int(ov["chat_id"])
        conn_id = ov["conn_id"]

        swaps = _build_swaps(
            chat_id,
            ov.get("tg_first_name"),
            ov.get("tg_last_name"),
            ov.get("nickname"),
        )
        for orig, alias in swaps.items():
            alias_summary[f"{chat_id}|{orig}"] = alias

        # History: last 30 messages oldest-first.
        cur.execute(
            "SELECT role, content FROM messages "
            "WHERE conn_id=? AND chat_id=? ORDER BY id DESC LIMIT 31",
            (conn_id, chat_id),
        )
        rows = list(cur.fetchall())
        if len(rows) < 2:
            continue  # not enough signal
        rows.reverse()
        # Use the LAST row as the contact_message if it's role=user, else find the most
        # recent role=user in the slice.
        contact_msg_row = None
        history_rows: list[sqlite3.Row] = []
        for r in rows:
            history_rows.append(r)
        # Walk backwards to find the most recent user message; everything before it is history.
        for i in range(len(history_rows) - 1, -1, -1):
            if history_rows[i]["role"] == "user":
                contact_msg_row = history_rows[i]
                history_rows = history_rows[:i]
                break
        if contact_msg_row is None:
            continue
        history = [
            {
                "role": r["role"],
                "content": _swap_names(_redact(r["content"]), swaps) or "",
            }
            for r in history_rows[-20:]  # keep last 20 history entries
        ]
        contact_message = _swap_names(_redact(contact_msg_row["content"]), swaps) or ""
        if not contact_message.strip():
            continue

        # Memory.
        cur.execute(
            "SELECT kind, content FROM contact_memory "
            "WHERE conn_id=? AND chat_id=? AND superseded_by IS NULL "
            "  AND (expires_at IS NULL OR expires_at > strftime('%s','now')) "
            "ORDER BY created_at DESC LIMIT 50",
            (conn_id, chat_id),
        )
        memory = [
            {
                "kind": m["kind"],
                "content": _swap_names(_redact(m["content"]), swaps) or "",
            }
            for m in cur.fetchall()
        ]

        # Summary (optional).
        cur.execute(
            "SELECT summary FROM chat_summaries WHERE conn_id=? AND chat_id=?",
            (conn_id, chat_id),
        )
        srow = cur.fetchone()
        summary = _swap_names(_redact(srow["summary"]), swaps) if srow else None

        scenario = {
            "name": _scenario_name(rel, chat_id),
            "relationship": rel,
            "owner_first_name": owner_first_name,
            "persona_extra": _swap_names(_redact(ov.get("persona_extra")), swaps),
            "style_fingerprint": ov.get("style_fingerprint"),
            "memory": memory,
            "summary": summary,
            "history": history,
            "contact_message": contact_message,
            "notes_for_judge": "",
        }

        out_path = out_dir / f"{scenario['name']}.json"
        out_path.write_text(json.dumps(scenario, ensure_ascii=False, indent=2), encoding="utf-8")
        written += 1
        counts[rel] = counts.get(rel, 0) + 1

    conn.close()
    print(f"wrote {written} scenarios to {out_dir}", file=sys.stderr)
    print(f"counts: {counts}", file=sys.stderr)
    return alias_summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source-db", type=Path, default=REPO_ROOT / "secretary.db")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "tests" / "fixtures" / "scenarios")
    ap.add_argument("--limit-per-relationship", type=int, default=3)
    ap.add_argument("--owner-first-name", type=str, default="Sepehr")
    args = ap.parse_args()

    if not args.source_db.exists():
        print(f"ERROR: source DB not found at {args.source_db}", file=sys.stderr)
        sys.exit(2)

    alias_map = dump(args.source_db, args.out, args.limit_per_relationship, args.owner_first_name)
    if alias_map:
        print("\nalias map (chat_id|original -> alias):", file=sys.stderr)
        for k, v in sorted(alias_map.items()):
            print(f"  {k} -> {v}", file=sys.stderr)


if __name__ == "__main__":
    main()
