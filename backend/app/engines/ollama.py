"""Ollama (llama.cpp por dentro): modelos de su librería (qwen3:8b…). Es el motor por defecto en el PC.

RAM o VRAM: Ollama reparte las capas solo (auto). Con la ubicación del modelo se fuerza con num_gpu:
GPU = todas las capas en VRAM, CPU = todo en RAM, Mixto = N capas en la GPU y el resto en RAM.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from .. import config
from .base import Engine

_client = httpx.AsyncClient(base_url=config.OLLAMA_URL, timeout=httpx.Timeout(900, connect=10))
_caps: dict[str, set[str]] = {}
_up: dict[str, float] = {}


def placement_options(placement: str, gpu_layers: int | None) -> dict:
    if placement == "cpu":
        return {"num_gpu": 0}
    if placement == "gpu":
        return {"num_gpu": 999}
    if placement == "mixed":
        return {"num_gpu": max(0, int(gpu_layers or 0))}
    return {}


async def capabilities(model: str) -> set[str]:
    if model not in _caps:
        try:
            r = await _client.post("/api/show", json={"model": model})
            r.raise_for_status()
            caps = set(r.json().get("capabilities") or [])
            _caps[model] = caps or {"completion", "tools"}
        except (httpx.HTTPError, ValueError):
            return {"completion", "tools"}
    return _caps[model]


def options(extra: dict | None) -> dict:
    opts = {"num_ctx": config.NUM_CTX, "temperature": config.TEMPERATURE}
    opts.update(extra or {})
    return opts


async def payload(model: str, messages: list[dict], tools: list[dict] | None, opts: dict | None,
                  stream: bool, fmt: Any = None) -> dict:
    caps = await capabilities(model)
    p: dict[str, Any] = {"model": model, "messages": messages, "stream": stream,
                         "keep_alive": config.KEEP_ALIVE, "options": options(opts)}
    if tools and "tools" in caps:
        p["tools"] = tools
    if "thinking" in caps:
        p["think"] = ("medium" if config.THINK else "low") if model.startswith("gpt-oss") else config.THINK
    if fmt is not None:
        p["format"] = fmt
    return p


class Ollama(Engine):
    id = "ollama"
    name = "llama.cpp (Ollama)"
    formats = ("ollama",)
    homepage = "https://ollama.com/download"

    def __init__(self):
        self.online = False

    def available(self, hw):
        return (True, "") if self.online else (False, "Ollama no está en marcha")

    async def probe(self) -> bool:
        try:
            r = await _client.get("/api/version", timeout=3)
            self.online = r.status_code == 200
        except httpx.HTTPError:
            self.online = False
        return self.online

    def placements(self, hw, spec):
        return ["gpu", "mixed", "cpu"] if hw["has_gpu"] else ["cpu"]

    async def chat_events(self, spec, messages, tools, options_):
        model = spec["ref"]
        opts = {**placement_options(spec.get("placement", "auto"), spec.get("gpu_layers")), **(options_ or {})}
        content, calls = [], []
        async with _client.stream("POST", "/api/chat",
                                  json=await payload(model, messages, tools, opts, True)) as r:
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
        yield "message", msg

    async def chat(self, spec: dict, messages: list[dict], tools, options_, fmt=None) -> dict:
        opts = {**placement_options(spec.get("placement", "auto"), spec.get("gpu_layers")), **(options_ or {})}
        r = await _client.post("/api/chat", json=await payload(spec["ref"], messages, tools, opts, False, fmt))
        r.raise_for_status()
        msg = r.json()["message"]
        msg.pop("thinking", None)
        return msg

    async def unload_model(self, model: str) -> None:
        try:
            await _client.post("/api/chat", json={"model": model, "messages": [], "keep_alive": 0})
        except httpx.HTTPError:
            pass


def client() -> httpx.AsyncClient:
    return _client
