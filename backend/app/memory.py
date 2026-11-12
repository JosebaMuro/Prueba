"""Memoria persistente en dos capas (inspirado en Mem0 y Letta/MemGPT):

1. Memoria episódica: conversaciones y mensajes en SQLite (data/studio.db).
2. Memoria semántica a largo plazo: hechos/preferencias + fragmentos de documentos del
   workspace en Qdrant (embebido en disco o servidor), recuperados por similitud.

Tras cada turno, `extract_and_store` pide al LLM que destile hechos duraderos y los
fusiona evitando duplicados (si un hecho nuevo es casi idéntico a uno existente, se actualiza).
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (Distance, FieldCondition, Filter, MatchValue, PointStruct,
                                  VectorParams)

from . import config, llm

DB_PATH = config.DATA_DIR / "studio.db"
# Una colección por modelo de embeddings: si cambias MODEL_EMBED, las dimensiones no chocan
# (la memoria antigua queda guardada y vuelve a estar disponible si recuperas el modelo anterior).
COLLECTION = "memoria_" + re.sub(r"[^a-z0-9]+", "_", config.MODEL_EMBED.lower()).strip("_")
DEDUP_THRESHOLD = 0.92

# ---------------------------------------------------------------- SQLite (episódica)


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")  # lecturas y escrituras concurrentes sin bloqueos
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations(
                id TEXT PRIMARY KEY, title TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT,
                role TEXT, agent TEXT, content TEXT, images TEXT, created REAL);
            CREATE INDEX IF NOT EXISTS ix_msg_conv ON messages(conversation_id, id);
            """
        )


def new_conversation(title: str = "Nueva conversación") -> str:
    cid = uuid.uuid4().hex[:12]
    with db() as c:
        c.execute("INSERT INTO conversations VALUES(?,?,?)", (cid, title, time.time()))
    return cid


def list_conversations() -> list[dict]:
    with db() as c:
        rows = c.execute("SELECT * FROM conversations ORDER BY created DESC").fetchall()
    return [dict(r) for r in rows]


def rename_conversation(cid: str, title: str) -> None:
    with db() as c:
        c.execute("UPDATE conversations SET title=? WHERE id=?", (title, cid))


def delete_conversation(cid: str) -> None:
    with db() as c:
        c.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
        c.execute("DELETE FROM conversations WHERE id=?", (cid,))


def add_message(cid: str, role: str, content: str, agent: str = "", images: list[str] | None = None) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO messages(conversation_id, role, agent, content, images, created) VALUES(?,?,?,?,?,?)",
            (cid, role, agent, content, json.dumps(images or []), time.time()),
        )


def get_messages(cid: str, limit: int = 200) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT role, agent, content, images, created FROM messages WHERE conversation_id=? "
            "ORDER BY id DESC LIMIT ?", (cid, limit)).fetchall()
    out = [dict(r) for r in reversed(rows)]
    for m in out:
        m["images"] = json.loads(m["images"] or "[]")
    return out


# ---------------------------------------------------------------- Qdrant (semántica)

_qdrant: AsyncQdrantClient | None = None
_dim: int | None = None
_init_lock = asyncio.Lock()


async def _client() -> AsyncQdrantClient:
    if _qdrant is not None and _dim is not None:
        return _qdrant
    async with _init_lock:
        return await _init_client()


async def _init_client() -> AsyncQdrantClient:
    global _qdrant, _dim
    if _qdrant is None:
        if config.QDRANT_URL:
            _qdrant = AsyncQdrantClient(url=config.QDRANT_URL)
        else:
            _qdrant = AsyncQdrantClient(path=str(config.DATA_DIR / "qdrant"))
    if _dim is None:
        _dim = len((await llm.embed(["dimensión"]))[0])
        if not await _qdrant.collection_exists(COLLECTION):
            await _qdrant.create_collection(
                COLLECTION, vectors_config=VectorParams(size=_dim, distance=Distance.COSINE))
    return _qdrant


async def remember(text: str, kind: str = "hecho", source: str = "usuario", meta: dict | None = None) -> dict:
    """Guarda un recuerdo. Si ya existe uno casi idéntico, lo actualiza en vez de duplicar."""
    q = await _client()
    vec = (await llm.embed([text], "doc"))[0]
    hits = (await q.query_points(COLLECTION, query=vec, limit=1,
                                 query_filter=Filter(must=[FieldCondition(key="kind", match=MatchValue(value=kind))]))).points
    pid = hits[0].id if hits and hits[0].score >= DEDUP_THRESHOLD else uuid.uuid4().hex
    payload = {"text": text, "kind": kind, "source": source, "updated": time.time(), **(meta or {})}
    await q.upsert(COLLECTION, points=[PointStruct(id=pid, vector=vec, payload=payload)])
    return {"id": str(pid), **payload, "updated_existing": bool(hits and hits[0].score >= DEDUP_THRESHOLD)}


async def recall(query: str, limit: int = 6, kind: str | None = None, min_score: float = 0.35) -> list[dict]:
    q = await _client()
    vec = (await llm.embed([query], "query"))[0]
    flt = Filter(must=[FieldCondition(key="kind", match=MatchValue(value=kind))]) if kind else None
    res = await q.query_points(COLLECTION, query=vec, limit=limit, query_filter=flt)
    return [{"id": str(p.id), "score": round(p.score, 3), **p.payload} for p in res.points if p.score >= min_score]


async def list_memories(kind: str | None = None, limit: int = 500) -> list[dict]:
    q = await _client()
    flt = Filter(must=[FieldCondition(key="kind", match=MatchValue(value=kind))]) if kind else None
    points, _ = await q.scroll(COLLECTION, scroll_filter=flt, limit=limit)
    items = [{"id": str(p.id), **p.payload} for p in points]
    return sorted(items, key=lambda m: m.get("updated", 0), reverse=True)


async def forget(memory_id: str) -> None:
    q = await _client()
    await q.delete(COLLECTION, points_selector=[memory_id])


async def forget_source(source: str) -> None:
    q = await _client()
    await q.delete(COLLECTION, points_selector=Filter(
        must=[FieldCondition(key="source", match=MatchValue(value=source))]))


# ---------------------------------------------------------------- Indexado de documentos (RAG)


def _chunks(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    """Trocea respetando párrafos y frases cuando es posible (mejores fragmentos = mejor búsqueda)."""
    out, i, n = [], 0, len(text)
    while i < n:
        end = min(i + size, n)
        if end < n:
            window = text[i + size // 2:end]
            for sep in ("\n\n", "\n", ". ", " "):
                k = window.rfind(sep)
                if k >= 0:
                    end = i + size // 2 + k + len(sep)
                    break
        out.append(text[i:end].strip())
        if end >= n:
            break
        i = max(end - overlap, i + 1)
    return [c for c in out if c]


async def index_document(rel_path: str, text: str) -> int:
    """Trocea e indexa un archivo del workspace para que los agentes puedan buscarlo."""
    q = await _client()
    source = f"workspace:{rel_path}"
    await forget_source(source)
    chunks = _chunks(text)
    if not chunks:
        return 0
    vecs = await llm.embed(chunks, "doc")  # en lotes de 32
    points = [PointStruct(id=uuid.uuid4().hex, vector=v,
                          payload={"text": c, "kind": "documento", "source": source,
                                   "path": rel_path, "chunk": i, "updated": time.time()})
              for i, (c, v) in enumerate(zip(chunks, vecs))]
    await q.upsert(COLLECTION, points=points)
    return len(points)


# ---------------------------------------------------------------- Extracción automática

EXTRACT_PROMPT = """Eres el módulo de memoria de un asistente. Lee el intercambio y extrae SOLO
hechos duraderos y útiles en futuras conversaciones sobre el usuario, sus proyectos,
preferencias, decisiones o datos importantes. Ignora saludos y detalles efímeros.
Devuelve JSON: {"hechos": ["...", "..."]} (lista vacía si no hay nada). Cada hecho
debe ser una frase autocontenida en tercera persona."""


_FACTS_SCHEMA = {"type": "object", "properties": {"hechos": {"type": "array", "items": {"type": "string"}}},
                 "required": ["hechos"]}


async def extract_and_store(user_text: str, assistant_text: str) -> list[str]:
    if len(user_text.strip()) < 12:  # "hola", "gracias"...: nada que recordar, nos ahorramos la llamada
        return []
    try:
        msg = await llm.chat(
            config.MODEL_CHAT,
            [{"role": "system", "content": EXTRACT_PROMPT},
             {"role": "user", "content": f"USUARIO: {user_text[:4000]}\n\nASISTENTE: {assistant_text[:2000]}"}],
            options={"temperature": 0, "num_predict": 300},
            fmt=_FACTS_SCHEMA,  # salida JSON garantizada por Ollama (structured outputs)
        )
        content = msg.get("content", "")
        start, end = content.find("{"), content.rfind("}")
        facts = json.loads(content[start:end + 1]).get("hechos", []) if start >= 0 else []
    except Exception:  # la extracción nunca debe romper el chat
        return []
    stored = []
    for f in facts[:5]:
        if isinstance(f, str) and len(f) > 8:
            await remember(f, kind="hecho", source="auto")
            stored.append(f)
    return stored
