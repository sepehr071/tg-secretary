"""One-shot migration: strip the dead `recurring_phrases` field from any
`contact_overrides.style_fingerprint` JSON blob.

Why this exists: an earlier version of `memory.py:STYLE_SYSTEM` asked the
extractor to capture "recurring_phrases". Those phrases were then re-injected
into the system prompt as `## Style`, which made the reply model treat them
as owner-voice instructions — a feedback loop that re-encouraged its own
prior outputs (including phrases the persona files explicitly ban, e.g.
"باو"). Commit 128b1f2 removed the field from the extractor schema, but
existing rows in the DB still carry it and silently leak into every reply.

Usage:
  uv run python scripts/strip_recurring_phrases.py --db <path> [--dry-run]

Idempotent: rows without the field are untouched.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, required=True, help="path to the SQLite DB")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, don't write")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"ERROR: db not found at {args.db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute(
        "SELECT chat_id, conn_id, relationship, style_fingerprint "
        "FROM contact_overrides WHERE style_fingerprint IS NOT NULL"
    )
    rows = cur.fetchall()

    changed = 0
    skipped = 0
    parse_errors = 0

    for row in rows:
        raw = row["style_fingerprint"]
        try:
            sf = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"  parse error chat_id={row['chat_id']}: {e}", file=sys.stderr)
            parse_errors += 1
            continue

        if "recurring_phrases" not in sf:
            skipped += 1
            continue

        rp = sf.pop("recurring_phrases")
        new_raw = json.dumps(sf, ensure_ascii=False)
        rel = row["relationship"] or "?"
        print(
            f"  chat_id={row['chat_id']} rel={rel} stripped {len(rp) if isinstance(rp, list) else '?'} "
            f"phrase(s): {rp}"
        )
        if not args.dry_run:
            cur.execute(
                "UPDATE contact_overrides SET style_fingerprint=? "
                "WHERE conn_id=? AND chat_id=?",
                (new_raw, row["conn_id"], row["chat_id"]),
            )
        changed += 1

    if not args.dry_run:
        conn.commit()
    conn.close()

    verb = "would update" if args.dry_run else "updated"
    print(
        f"\n{verb} {changed} row(s); skipped {skipped} clean row(s); "
        f"{parse_errors} parse error(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
