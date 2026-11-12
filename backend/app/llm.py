"""Cliente para la API nativa de Ollama: chat (con herramientas y streaming), visión y embeddings.

Optimizaciones:
- Mismas opciones (num_ctx, keep_alive) en todas las llamadas: Ollama nunca recarga el modelo
  por un cambio de contexto y lo mantiene en memoria entre mensajes.
- Capacidades de cada modelo (tools / thinking / vision) consultadas una vez a /api/show y
  cacheadas: no se envía `think` ni `tools` a modelos que no los admiten (evita errores 400).
- Respuestas en streaming con herramientas: el texto llega a la interfaz según se genera.
- Embeddings con el prefijo que cada modelo espera para consultas y documentos, y caché LRU.
- Rescate de llamadas a herramientas que algunos modelos escriben como texto en vez de tool_calls.
"""
from __future__ import annotations

import json
import re
import uuid
from collections import OrderedDict
from typing import Any, AsyncIterator

import httpx

from . import config

_client = httpx.AsyncClient(base_url=config.OLLAMA_URL, timeout=httpx.Timeout(900, connect=10))
_caps: dict[str, set[str]] = {}


async def capabilities(model: str) -> set[str]:
    """Capacidades declaradas por Ollama para el modelo (vacío si no se pueden consultar)."""
    if model not in _caps:
        try:
            r = await _client.post("/api/show", json={"model": model})
            r.raise_for_status()
            caps = set(r.json().get("capabilities") or [])
            _caps[model] = caps or {"completion", "tools"}  # Ollama antiguo: no informa, asumimos tools
        except (httpx.HTTPError, ValueError):
            return {"completion", "tools"}  # sin cachear: se reintenta en la próxima llamada
    return _caps[model]


def _options(extra: dict | None) -> dict:
    opts = {"num_ctx": config.NUM_CTX, "temperature": config.TEMPERATURE}
    opts.update(extra or {})
    return opts


async def _payload(model: str, messages: list[dict], tools: list[dict] | None, options: dict | None,
                   stream: bool, fmt: Any = None) -> dict:
    caps = await capabilities(model)
    payload: dict[str, Any] = {"model": model, "messages": messages, "stream": stream,
                               "keep_alive": config.KEEP_ALIVE, "options": _options(options)}
    if tools and "tools" in caps:
        payload["tools"] = tools
    if "thinking" in caps:
        # gpt-oss no se puede desactivar: se le pide el nivel mínimo
        payload["think"] = ("medium" if config.THINK else "low") if model.startswith("gpt-oss") else config.THINK
    if fmt is not None:
        payload["format"] = fmt
    return payload


_TOOL_TAG = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


def rescue_tool_calls(msg: dict, tools: list[dict] | None) -> dict:
    """Si el modelo escribió la llamada a herramienta como texto, la convierte en tool_calls."""
    if msg.get("tool_calls") or not tools:
        return msg
    content = (msg.get("content") or "").strip()
    names = {t["function"]["name"] for t in tools}
    candidates = _TOOL_TAG.findall(content)
    if not candidates:
        bare = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        if bare.startswith("{") and bare.endswith("}"):
            candidates = [bare]
    calls = []
    for c in candidates:
        try:
            j = json.loads(c)
        except ValueError:
            continue
        name = j.get("name") or (j.get("function") or {}).get("name")
        args = j.get("arguments", j.get("parameters", j.get("args", {})))
        if name in names:
            calls.append({"function": {"name": name, "arguments": args if isinstance(args, dict) else {}}})
    if calls:
        msg = {**msg, "content": "", "tool_calls": calls}
    return msg


async def chat(model: str, messages: list[dict], tools: list[dict] | None = None,
               options: dict | None = None, fmt: Any = None) -> dict:
    """Llamada no streaming. Devuelve el mensaje del asistente (puede traer tool_calls)."""
    r = await _client.post("/api/chat", json=await _payload(model, messages, tools, options, False, fmt))
    r.raise_for_status()
    msg = r.json()["message"]
    msg.pop("thinking", None)
    return rescue_tool_calls(msg, tools)


async def chat_events(model: str, messages: list[dict], tools: list[dict] | None = None,
                      options: dict | None = None) -> AsyncIterator[tuple[str, Any]]:
    """Streaming con herramientas. Emite ("text", trozo) según se genera y al final ("message", msg)."""
    content, calls = [], []
    async with _client.stream("POST", "/api/chat",
                              json=await _payload(model, messages, tools, options, True)) as r:
        if r.status_code >= 400:
            await r.aread()
            r.raise_for_status()
        async for line in r.aiter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            if chunk.get("error"):
                raise RuntimeError(chunk["error"])
            m = chunk.get("message") or {}
            if m.get("tool_calls"):
                calls.extend(m["tool_calls"])
            piece = m.get("content") or ""
            if piece:
                content.append(piece)
                yield "text", piece
            if chunk.get("done"):
                break
    msg = {"role": "assistant", "content": "".join(content)}
    if calls:
        msg["tool_calls"] = calls
    yield "message", rescue_tool_calls(msg, tools)


async def chat_stream(model: str, messages: list[dict], options: dict | None = None) -> AsyncIterator[str]:
    async for kind, val in chat_events(model, messages, None, options):
        if kind == "text":
            yield val


# ---------------------------------------------------------------- embeddings

_EMB_CACHE: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
_EMB_CACHE_MAX = 2048


def _prefix(kind: str) -> str:
    """Prefijos recomendados por cada familia de modelos de embeddings (mejoran mucho la recuperación)."""
    m = config.MODEL_EMBED.lower()
    if "nomic" in m:
        return "search_query: " if kind == "query" else "search_document: "
    if "qwen3-embedding" in m:
        return ("Instruct: Given a question, retrieve facts and passages that answer it\nQuery: "
                if kind == "query" else "")
    if "embeddinggemma" in m:
        return "task: search result | query: " if kind == "query" else "title: none | text: "
    return ""


async def embed(texts: list[str], kind: str = "doc", batch: int = 32) -> list[list[float]]:
    """kind = "query" (lo que se busca) o "doc" (lo que se guarda)."""
    out: list[list[float] | None] = [None] * len(texts)
    todo: list[int] = []
    for i, t in enumerate(texts):
        hit = _EMB_CACHE.get((kind, t))
        if hit is not None:
            _EMB_CACHE.move_to_end((kind, t))
            out[i] = hit
        else:
            todo.append(i)
    pre = _prefix(kind)
    for s in range(0, len(todo), batch):
        idx = todo[s:s + batch]
        r = await _client.post("/api/embed", json={"model": config.MODEL_EMBED, "keep_alive": config.KEEP_ALIVE,
                                                   "input": [pre + texts[i] for i in idx]})
        r.raise_for_status()
        for i, v in zip(idx, r.json()["embeddings"]):
            out[i] = v
            _EMB_CACHE[(kind, texts[i])] = v
            if len(_EMB_CACHE) > _EMB_CACHE_MAX:
                _EMB_CACHE.popitem(last=False)
    return out  # type: ignore[return-value]


# ---------------------------------------------------------------- utilidades

async def list_models() -> list[str]:
    try:
        r = await _client.get("/api/tags")
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
    except httpx.HTTPError:
        return []


async def running_models() -> list[dict]:
    """Modelos cargados ahora mismo y cuánta memoria de GPU usan (para el panel Modelos)."""
    try:
        r = await _client.get("/api/ps")
        r.raise_for_status()
        return [{"name": m["name"], "size": m.get("size", 0), "vram": m.get("size_vram", 0),
                 "ctx": m.get("context_length")} for m in r.json().get("models", [])]
    except (httpx.HTTPError, ValueError):
        return []


async def warmup() -> None:
    """Carga el modelo de chat y el de embeddings en segundo plano para que el primer mensaje sea rápido."""
    try:
        await _client.post("/api/chat", json={"model": config.MODEL_CHAT, "messages": [],
                                              "keep_alive": config.KEEP_ALIVE, "options": _options(None)})
        await embed([f"warmup {uuid.uuid4().hex[:4]}"])
    except Exception:
        pass
