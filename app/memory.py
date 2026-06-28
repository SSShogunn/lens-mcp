import asyncio
import logging
import math
import os
import sqlite3
from array import array
from datetime import datetime, timezone

logger = logging.getLogger("lens.memory")

DB_PATH = os.environ.get("LENS_MEMORY_DB_PATH", "data/memory.db")
EMBEDDING_MODEL = os.environ.get("LENS_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")

_fastembed_model = None

_conn: sqlite3.Connection | None = None
_lock = asyncio.Lock()


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory (
            name TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            description TEXT NOT NULL,
            content TEXT NOT NULL,
            embedding BLOB,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _upsert(
    conn: sqlite3.Connection,
    name: str,
    type_: str,
    description: str,
    content: str,
    embedding_blob: bytes,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO memory (name, type, description, content, embedding, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            type = excluded.type,
            description = excluded.description,
            content = excluded.content,
            embedding = excluded.embedding,
            updated_at = excluded.updated_at
        """,
        (name, type_, description, content, embedding_blob, now, now),
    )
    conn.commit()


def _fetch_all(conn: sqlite3.Connection, type_: str | None) -> list[tuple]:
    if type_:
        cur = conn.execute(
            "SELECT name, type, description, content, embedding, updated_at FROM memory WHERE type = ?",
            (type_,),
        )
    else:
        cur = conn.execute(
            "SELECT name, type, description, content, embedding, updated_at FROM memory"
        )
    return cur.fetchall()


def _list(conn: sqlite3.Connection, type_: str | None) -> list[tuple]:
    if type_:
        cur = conn.execute(
            "SELECT name, type, description, updated_at FROM memory WHERE type = ? ORDER BY updated_at DESC",
            (type_,),
        )
    else:
        cur = conn.execute(
            "SELECT name, type, description, updated_at FROM memory ORDER BY updated_at DESC"
        )
    return cur.fetchall()


def _delete(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute("DELETE FROM memory WHERE name = ?", (name,))
    conn.commit()
    return cur.rowcount > 0


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _fastembed_embed(text: str) -> list[float]:
    global _fastembed_model
    if _fastembed_model is None:
        from fastembed import TextEmbedding
        _fastembed_model = TextEmbedding(EMBEDDING_MODEL)
    return next(iter(_fastembed_model.embed([text]))).tolist()


async def _embed(text: str) -> list[float]:
    return await asyncio.to_thread(_fastembed_embed, text)


async def save(name: str, type: str, description: str, content: str) -> dict:
    embedding = await _embed(f"{description}\n\n{content}")
    blob = array("f", embedding).tobytes()
    now = datetime.now(timezone.utc).isoformat()
    assert _conn is not None
    async with _lock:
        await asyncio.to_thread(_upsert, _conn, name, type, description, content, blob, now)
    return {"name": name, "type": type, "description": description, "updated_at": now}


async def search(query: str, top_k: int = 5, type: str | None = None) -> list[dict]:
    query_embedding = await _embed(query)
    assert _conn is not None
    async with _lock:
        rows = await asyncio.to_thread(_fetch_all, _conn, type)

    scored = []
    for name, type_, description, content, blob, updated_at in rows:
        if not blob:
            continue
        vec = array("f")
        vec.frombytes(blob)
        score = _cosine(query_embedding, list(vec))
        scored.append(
            {
                "name": name,
                "type": type_,
                "description": description,
                "content": content,
                "score": score,
                "updated_at": updated_at,
            }
        )
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:top_k]


async def list_entries(type: str | None = None) -> list[dict]:
    assert _conn is not None
    async with _lock:
        rows = await asyncio.to_thread(_list, _conn, type)
    return [
        {"name": name, "type": type_, "description": description, "updated_at": updated_at}
        for name, type_, description, updated_at in rows
    ]


async def delete(name: str) -> bool:
    assert _conn is not None
    async with _lock:
        return await asyncio.to_thread(_delete, _conn, name)


async def start() -> None:
    global _conn
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    _conn = await asyncio.to_thread(sqlite3.connect, DB_PATH, check_same_thread=False)
    await asyncio.to_thread(_init_db, _conn)
    logger.info("Memory store started (db=%s, embeddings=fastembed:%s)", DB_PATH, EMBEDDING_MODEL)


async def stop() -> None:
    global _conn
    if _conn is not None:
        await asyncio.to_thread(_conn.close)
        _conn = None
    logger.info("Memory store stopped")
