"""One-shot purge: soft-expire ALL active contact_memory rows.

Why this exists: until the memory.py speaker-labeling fix, the extractor saw
owner-typed and bot-sent messages both as bare `assistant:` lines, so it stored
OWNER facts as the contact's (the owner's own name, possessions, even
bot-fabricated banter canonized as conf=1.0 "facts"). Every pre-fix memory row
is therefore suspect. This script wipes the poisoned corpus the same way the
app "deletes" memory — soft-expire (`expires_at = now`), matching the active-row
predicate of `db.list_memory` — so the worker rebuilds clean memory from new
messages going forward.

Run this on the production DB AFTER deploying the memory.py fix; running it
before redeploy means the still-buggy extractor re-poisons within one cycle.

Usage:
  PYTHONIOENCODING=utf-8 uv run python scripts/expire_all_memory.py --db <path> [--dry-run]

Idempotent: only currently-active rows match (already-expired / superseded rows
are untouched), so a second run reports 0.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, required=True, help="path to the SQLite DB")
    ap.add_argument("--dry-run", action="store_true", help="show what would expire, don't write")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"ERROR: db not found at {args.db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    now = int(time.time())
    cur.execute(
        "SELECT id, chat_id, kind, content FROM contact_memory "
        "WHERE superseded_by IS NULL AND (expires_at IS NULL OR expires_at > ?) "
        "ORDER BY chat_id, id",
        (now,),
    )
    rows = cur.fetchall()

    for row in rows:
        print(f"  chat_id={row['chat_id']} [{row['kind']}] {row['content'][:80]}")

    if rows and not args.dry_run:
        cur.execute(
            "UPDATE contact_memory SET expires_at = ? "
            "WHERE superseded_by IS NULL AND (expires_at IS NULL OR expires_at > ?)",
            (now, now),
        )
        conn.commit()
    conn.close()

    verb = "would expire" if args.dry_run else "expired"
    print(f"\n{verb} {len(rows)} active memory row(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
