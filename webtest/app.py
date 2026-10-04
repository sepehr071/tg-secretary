"""FastAPI app for the local secretary replay tester.

Single-user, localhost-only. Uploads land in webtest/uploads/<uuid>.db. Every
request that touches the DB swaps settings.db_path to the uploaded file and
calls db.init_db() under an asyncio lock so we never race the module-global
secretary.db._conn.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from secretary import db
from secretary.config import settings

from . import replay

log = logging.getLogger("webtest")

WEBTEST_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = WEBTEST_DIR / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB
SQLITE_MAGIC = b"SQLite format 3\x00"

app = FastAPI(title="tg-secretary web tester")
templates = Jinja2Templates(directory=str(WEBTEST_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(WEBTEST_DIR / "static")), name="static")

# Single mutex around db._conn swap. Local single-user, so contention is rare,
# but two overlapping requests on different uploads must not stomp each other.
_DB_LOCK = asyncio.Lock()
_CURRENT_UPLOAD_PATH: Path | None = None


def _upload_path(upload_id: str) -> Path:
    # Defend against traversal: upload_id must be a hex uuid.
    try:
        uuid.UUID(upload_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="bad upload_id") from e
    path = UPLOADS_DIR / f"{upload_id}.db"
    if not path.exists():
        raise HTTPException(status_code=404, detail="upload not found")
    return path


async def _attach_db(upload_path: Path) -> None:
    """Point settings.db_path at the uploaded file and (re-)open db._conn."""
    global _CURRENT_UPLOAD_PATH
    if _CURRENT_UPLOAD_PATH == upload_path and db._conn is not None:
        return
    if db._conn is not None:
        await db.close_db()
    settings.db_path = upload_path
    await db.init_db()
    _CURRENT_UPLOAD_PATH = upload_path


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"default_model": settings.openrouter_model},
    )


@app.post("/upload")
async def upload(file: UploadFile):
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"file > {MAX_UPLOAD_BYTES} bytes")
    if not data.startswith(SQLITE_MAGIC):
        raise HTTPException(status_code=400, detail="not a SQLite database (bad magic bytes)")
    upload_id = uuid.uuid4().hex
    target = UPLOADS_DIR / f"{upload_id}.db"
    target.write_bytes(data)
    return {"upload_id": upload_id, "size_bytes": len(data), "filename": file.filename}


@app.get("/uploads/{upload_id}/chats")
async def list_chats(upload_id: str):
    path = _upload_path(upload_id)
    async with _DB_LOCK:
        await _attach_db(path)
        cur = await db._db().execute(
            """
            SELECT
                m.chat_id,
                m.conn_id,
                COUNT(*) AS msg_count,
                MIN(m.created_at) AS first_seen,
                MAX(m.created_at) AS last_seen,
                o.relationship,
                o.nickname,
                o.tg_first_name,
                o.tg_last_name,
                o.tg_username
            FROM messages m
            LEFT JOIN contact_overrides o
                ON o.chat_id = m.chat_id AND o.conn_id = m.conn_id
            GROUP BY m.conn_id, m.chat_id
            ORDER BY last_seen DESC
            """
        )
        rows = await cur.fetchall()
    chats = []
    for r in rows:
        nick = r["nickname"]
        full = " ".join(p for p in (r["tg_first_name"], r["tg_last_name"]) if p)
        uname = r["tg_username"]
        display = nick or full or (f"@{uname}" if uname else None) or str(r["chat_id"])
        chats.append({
            "chat_id": r["chat_id"],
            "conn_id": r["conn_id"],
            "msg_count": r["msg_count"],
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "relationship": r["relationship"] or "untagged",
            "display": display,
            "tg_username": uname,
        })
    return {"chats": chats}


@app.get("/uploads/{upload_id}/chats/{chat_id}/messages")
async def list_messages(
    upload_id: str,
    chat_id: int,
    before_id: int | None = None,
    limit: int = 200,
):
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit must be 1..1000")
    path = _upload_path(upload_id)
    async with _DB_LOCK:
        await _attach_db(path)
        params: list[Any] = [chat_id]
        sql = (
            "SELECT id, role, content, via_bot, created_at FROM messages "
            "WHERE chat_id = ?"
        )
        if before_id is not None:
            sql += " AND id < ?"
            params.append(before_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        cur = await db._db().execute(sql, params)
        rows = await cur.fetchall()
    msgs = [
        {
            "id": r["id"],
            "role": r["role"],
            "content": r["content"],
            "via_bot": bool(r["via_bot"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    msgs.reverse()  # caller renders oldest-first
    return {"messages": msgs, "has_more": len(msgs) == limit}


async def _replay_sse(
    upload_path: Path,
    chat_id: int,
    start_msg_id: int,
    end_msg_id: int,
    columns: list[replay.Column],
) -> AsyncIterator[bytes]:
    async with _DB_LOCK:
        await _attach_db(upload_path)
        try:
            async for ev in replay.run_replay(
                chat_id=chat_id,
                start_msg_id=start_msg_id,
                end_msg_id=end_msg_id,
                columns=columns,
            ):
                event_name, payload = replay.event_to_jsonable(ev)
                data = json.dumps(payload, ensure_ascii=False)
                yield f"event: {event_name}\ndata: {data}\n\n".encode("utf-8")
        except Exception as e:  # noqa: BLE001
            err = json.dumps({"fatal": f"{type(e).__name__}: {e}"})
            yield f"event: fatal\ndata: {err}\n\n".encode("utf-8")
        finally:
            yield b"event: done\ndata: {}\n\n"


MAX_COLUMNS = 3


@app.post("/uploads/{upload_id}/replay")
async def start_replay(upload_id: str, body: dict[str, Any]):
    path = _upload_path(upload_id)
    try:
        chat_id = int(body["chat_id"])
        start_msg_id = int(body["start_msg_id"])
        end_msg_id = int(body["end_msg_id"])
        raw_columns = body.get("columns") or []
    except (KeyError, ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"bad body: {e}") from e
    if not isinstance(raw_columns, list) or not raw_columns:
        raise HTTPException(status_code=400, detail="columns must be a non-empty list")
    if len(raw_columns) > MAX_COLUMNS:
        raise HTTPException(status_code=400, detail=f"max {MAX_COLUMNS} columns")
    if end_msg_id < start_msg_id:
        raise HTTPException(status_code=400, detail="end_msg_id < start_msg_id")

    columns: list[replay.Column] = []
    for idx, c in enumerate(raw_columns):
        if not isinstance(c, dict):
            raise HTTPException(status_code=400, detail=f"column[{idx}] not an object")
        model = str(c.get("model") or "").strip()
        if not model:
            raise HTTPException(status_code=400, detail=f"column[{idx}].model required")
        effort_raw = c.get("reasoning_effort")
        effort = str(effort_raw).strip().lower() if effort_raw else None
        if effort in ("", "default"):
            effort = None
        columns.append(replay.Column(idx=idx, model=model, reasoning_effort=effort))

    headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    return StreamingResponse(
        _replay_sse(path, chat_id, start_msg_id, end_msg_id, columns),
        media_type="text/event-stream",
        headers=headers,
    )


@app.get("/healthz")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True})
