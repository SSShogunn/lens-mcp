import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger("lens.db")

DB_PATH = os.environ.get("LENS_DB_PATH", "data/lens.db")
MAX_FIELD_CHARS = 8000

_queue: asyncio.Queue | None = None
_worker: asyncio.Task | None = None


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            tool TEXT NOT NULL,
            arguments TEXT,
            status TEXT NOT NULL,
            response TEXT,
            error TEXT,
            duration_ms REAL
        )
        """
    )
    conn.commit()


def _insert(conn: sqlite3.Connection, record: tuple) -> None:
    conn.execute(
        "INSERT INTO requests "
        "(timestamp, tool, arguments, status, response, error, duration_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        record,
    )
    conn.commit()


async def _run_worker() -> None:
    assert _queue is not None
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = await asyncio.to_thread(sqlite3.connect, DB_PATH, check_same_thread=False)
    try:
        await asyncio.to_thread(_init_db, conn)
        while True:
            record = await _queue.get()
            try:
                if record is None:
                    break
                await asyncio.to_thread(_insert, conn, record)
            except Exception:
                logger.exception("Failed to write request log")
            finally:
                _queue.task_done()
    finally:
        await asyncio.to_thread(conn.close)


def _truncate(value: str | None) -> str | None:
    if value is None:
        return None
    return value[:MAX_FIELD_CHARS]


def log_request(
    tool: str,
    arguments,
    status: str,
    response: str | None = None,
    error: str | None = None,
    duration_ms: float | None = None,
) -> None:
    if _queue is None:
        return
    record = (
        datetime.now(timezone.utc).isoformat(),
        tool,
        _truncate(json.dumps(arguments, default=str)) if arguments is not None else None,
        status,
        _truncate(response),
        _truncate(error),
        duration_ms,
    )
    try:
        _queue.put_nowait(record)
    except asyncio.QueueFull:
        logger.warning("Request log queue full; dropping record for %s", tool)


async def start() -> None:
    global _queue, _worker
    _queue = asyncio.Queue(maxsize=1000)
    _worker = asyncio.create_task(_run_worker())
    logger.info("Request logging started (db=%s)", DB_PATH)


async def stop() -> None:
    global _queue, _worker
    if _queue is not None:
        await _queue.put(None)
    if _worker is not None:
        await _worker
    _queue = None
    _worker = None
    logger.info("Request logging stopped")
