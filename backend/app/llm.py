"""Puerta única a los modelos: chat (con herramientas y streaming), visión y embeddings.

El chat lo atiende el motor que elija engines.resolve() para cada modelo: Ollama para los nombres de su
librería (qwen3:8b) y llama-server, ONNX Runtime GenAI, OpenVINO GenAI, LiteRT-LM, ExecuTorch, MLC LLM,
TensorRT-LLM o Qualcomm Genie para modelos locales en sus formatos (ver engines/).
Los embeddings de la memoria siguen en Ollama (con prefijos por familia de modelo y caché LRU).
"""
from __future__ import annotations

import uuid
from collections import OrderedDict
from typing import Any, AsyncIterator

import httpx

from . import config, engines
from .engines import ollama as _ollama

_client = _ollama.client()


async def capabilities(model: str) -> set[str]:
    """Capacidades del modelo (tools / thinking / vision). Para Ollama se consultan; para el resto, por formato."""
    try:
        engine, spec = await engines.resolve(model)
    except engines.EngineUnavailable:
        return {"completion", "tools"}
    if engine.id == "ollama":
        return await _ollama.capabilities(model)
    caps = engines.capabilities_sync(model)
    return caps if engine.id == "llamacpp" else caps - {"vision"}


def rescue_tool_calls(msg: dict, tools: list[dict] | None) -> dict:
    """Si el modelo escribió la llamada a herramienta como texto, la convierte en tool_calls."""
    if msg.get("tool_calls") or not tools:
        return msg
    from .engines.base import parse_tool_calls
    _rest, calls = parse_tool_calls((msg.get("content") or "").strip(), tools)
    if calls:
        msg = {**msg, "content": "", "tool_calls": calls}
    return msg


async def chat(model: str, messages: list[dict], tools: list[dict] | None = None,
               options: dict | None = None, fmt: Any = None) -> dict:
    """Llamada no streaming. Devuelve el mensaje del asistente (puede traer tool_calls)."""
    engine, spec = await engines.resolve(model)
    if engine.id == "ollama":
        return rescue_tool_calls(await engine.chat(spec, messages, tools, options, fmt), tools)
    msg: dict = {}
    async for kind, val in engine.chat_events(spec, messages, tools, options):
        if kind == "message":
            msg = val
    if fmt is not None and not msg.get("tool_calls"):
        msg["content"] = _json_only(msg.get("content", ""))
    return rescue_tool_calls(msg, tools)


def _json_only(text: str) -> str:
    """Motores sin salida JSON forzada: se recorta el primer objeto/lista JSON del texto."""
    for a, b in (("{", "}"), ("[", "]")):
        i, j = text.find(a), text.rfind(b)
        if 0 <= i < j:
            return text[i:j + 1]
    return text


async def chat_events(model: str, messages: list[dict], tools: list[dict] | None = None,
                      options: dict | None = None) -> AsyncIterator[tuple[str, Any]]:
    """Streaming con herramientas. Emite ("text", trozo) según se genera y al final ("message", msg).
    El motor (Ollama, llama-server, ONNX Runtime GenAI, OpenVINO…) lo elige engines.resolve()."""
    engine, spec = await engines.resolve(model)
    async for kind, val in engine.chat_events(spec, messages, tools, options):
        yield kind, (rescue_tool_calls(val, tools) if kind == "message" else val)


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
        engine, _spec = await engines.resolve(config.MODEL_CHAT)
        if engine.id == "ollama":
            await _client.post("/api/chat", json={"model": config.MODEL_CHAT, "messages": [],
                                                  "keep_alive": config.KEEP_ALIVE, "options": _ollama.options(None)})
        await embed([f"warmup {uuid.uuid4().hex[:4]}"])
    except Exception:
        pass
