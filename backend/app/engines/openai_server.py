"""Motores que se usan como servidor compatible con OpenAI (/v1/chat/completions):

- llama.cpp (llama-server): archivos .gguf sueltos. RAM/VRAM con -ngl (capas en la GPU).
- MLC LLM (mlc_llm serve): modelos compilados con MLC (Vulkan, CUDA, Metal).
- TensorRT-LLM (trtllm-serve): GPUs NVIDIA; motores .engine o checkpoints de Hugging Face. Solo Linux/WSL.

El estudio arranca el servidor cuando hace falta (y lo cambia si cambia el modelo o su ubicación),
o usa uno que ya esté en marcha si se indica su URL (LLAMA_SERVER_URL, MLC_URL, TRTLLM_URL).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from .. import config
from .base import Engine, parse_tool_calls, text_events, with_tools_in_system, plain_messages


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServerEngine(Engine):
    url_setting = ""
    exe_names: tuple[str, ...] = ()

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.key: tuple | None = None
        self.base = ""
        self.lock = asyncio.Lock()
        self.native_tools = True

    # --- configuración
    def external_url(self) -> str:
        return getattr(config, self.url_setting, "") if self.url_setting else ""

    def executable(self) -> str:
        for n in self.exe_names:
            p = shutil.which(n)
            if p:
                return p
        return ""

    def command(self, spec: dict, port: int) -> list[str]:
        raise NotImplementedError

    def available(self, hw):
        if self.external_url() or self.executable():
            return True, ""
        return False, self.missing_reason(hw)

    def missing_reason(self, hw) -> str:
        return "No instalado"

    # --- servidor
    async def ensure(self, spec: dict) -> str:
        ext = self.external_url()
        if ext:
            return ext.rstrip("/")
        key = (spec["path"], spec.get("placement"), spec.get("gpu_layers"), config.NUM_CTX)
        async with self.lock:
            if self.proc and self.proc.poll() is None and self.key == key:
                return self.base
            await self.unload()
            port = _free_port()
            cmd = self.command(spec, port)
            log = open(config.DATA_DIR / f"engine-{self.id}.log", "w", encoding="utf-8", errors="replace")
            flags = 0x08000000 if sys.platform == "win32" else 0
            self.proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, creationflags=flags,
                                         env={**os.environ, "PYTHONUNBUFFERED": "1"})
            self.base, self.key, self.native_tools = f"http://127.0.0.1:{port}", key, True
            deadline = time.time() + 600
            async with httpx.AsyncClient(timeout=3) as c:
                while time.time() < deadline:
                    if self.proc.poll() is not None:
                        break
                    try:
                        r = await c.get(self.base + "/health")
                        if r.status_code == 200:
                            return self.base
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.5)
            tail = ""
            try:
                tail = (config.DATA_DIR / f"engine-{self.id}.log").read_text(errors="replace")[-1500:]
            except OSError:
                pass
            await self.unload()
            raise RuntimeError(f"{self.name} no arrancó.\n{tail}")

    async def unload(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc, self.key = None, None

    # --- chat
    async def chat_events(self, spec, messages, tools, options):
        base = await self.ensure(spec)
        native = bool(tools) and self.native_tools
        msgs = plain_messages(messages, native) if native else with_tools_in_system(plain_messages(messages, False), tools)
        _attach_images(msgs, messages)
        body = {"model": spec.get("served_name") or Path(spec["path"] or "model").name, "messages": msgs,
                "stream": True, "temperature": config.TEMPERATURE, "max_tokens": config.MAX_NEW_TOKENS}
        if native:
            body["tools"] = tools
        if "qwen3" in str(spec.get("path", "")).lower():
            body["chat_template_kwargs"] = {"enable_thinking": config.THINK}
        async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=10)) as c:
            async with c.stream("POST", base + "/v1/chat/completions", json=body) as r:
                if r.status_code == 400 and native:
                    # El servidor no admite herramientas nativas: se describen en el prompt
                    await r.aread()
                    self.native_tools = False
                    async for ev in self.chat_events(spec, messages, tools, options):
                        yield ev
                    return
                if r.status_code >= 400:
                    await r.aread()
                    raise RuntimeError(f"{self.name}: {r.status_code} {r.text[:500]}")
                if native:
                    async for ev in _native_events(r, tools):
                        yield ev
                else:
                    async for ev in text_events(_sse_text(r), tools):
                        yield ev


def _attach_images(msgs: list[dict], original: list[dict]) -> None:
    """Imágenes del último mensaje del usuario en formato OpenAI (llama-server con --mmproj las entiende)."""
    imgs = next((m.get("images") for m in reversed(original) if m.get("role") == "user"), None)
    if not imgs:
        return
    for m in reversed(msgs):
        if m["role"] == "user":
            m["content"] = [{"type": "text", "text": m["content"]}] + [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in imgs]
            return


async def _sse(r):
    async for line in r.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            yield json.loads(data)
        except ValueError:
            continue


async def _sse_text(r):
    async for chunk in _sse(r):
        delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
        if delta.get("content"):
            yield delta["content"]


async def _native_events(r, tools):
    content, calls = [], {}
    async for chunk in _sse(r):
        delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
        for tc in delta.get("tool_calls") or []:
            slot = calls.setdefault(tc.get("index", len(calls)), {"name": "", "arguments": ""})
            fn = tc.get("function") or {}
            slot["name"] += fn.get("name") or ""
            slot["arguments"] += fn.get("arguments") or ""
        piece = delta.get("content") or ""
        if piece:
            content.append(piece)
            if not calls:
                yield "text", piece
    msg: dict = {"role": "assistant", "content": "".join(content)}
    if calls:
        out = []
        for c in calls.values():
            try:
                args = json.loads(c["arguments"] or "{}")
            except ValueError:
                args = {}
            out.append({"function": {"name": c["name"], "arguments": args}})
        msg["tool_calls"] = out
    else:
        rest, parsed = parse_tool_calls(msg["content"], tools)
        if parsed:
            msg = {"role": "assistant", "content": "", "tool_calls": parsed}
    yield "message", msg


class LlamaServer(ServerEngine):
    id = "llamacpp"
    name = "llama.cpp (llama-server)"
    formats = ("gguf",)
    url_setting = "LLAMA_SERVER_URL"
    exe_names = ("llama-server", "llama-server.exe")
    homepage = "https://github.com/ggml-org/llama.cpp/releases"

    def executable(self):
        if config.LLAMA_SERVER and Path(config.LLAMA_SERVER).exists():
            return config.LLAMA_SERVER
        for p in (config.ENGINE_PACKS_DIR / "llama.cpp").glob("llama-server*"):
            return str(p)
        return super().executable()

    def missing_reason(self, hw):
        return ("Descarga llama.cpp (llama-server) y pon su ruta en LLAMA_SERVER, o déjalo en "
                f"{config.ENGINE_PACKS_DIR / 'llama.cpp'}. Los modelos de Ollama ya usan llama.cpp.")

    def placements(self, hw, spec):
        return ["gpu", "mixed", "cpu"] if hw["has_gpu"] else ["cpu"]

    def command(self, spec, port):
        pl = spec.get("placement", "auto")
        ngl = {"cpu": 0, "mixed": int(spec.get("gpu_layers") or 0)}.get(pl, 999)
        cmd = [self.executable(), "-m", spec["path"], "--host", "127.0.0.1", "--port", str(port),
               "-c", str(config.NUM_CTX), "-ngl", str(ngl), "--jinja", "--no-webui"]
        if spec.get("mmproj"):
            cmd += ["--mmproj", spec["mmproj"]]
        if pl == "cpu":
            cmd += ["--no-kv-offload"]
        return cmd


class MLC(ServerEngine):
    id = "mlc"
    name = "MLC LLM"
    formats = ("mlc",)
    url_setting = "MLC_URL"
    exe_names = ("mlc_llm", "mlc_llm.exe")
    install = ["--extra-index-url", "https://mlc.ai/wheels", "mlc-llm-nightly-cpu", "mlc-ai-nightly-cpu"]
    homepage = "https://llm.mlc.ai/docs/install/mlc_llm.html"

    def executable(self):
        import importlib.util
        if importlib.util.find_spec("mlc_llm"):
            return sys.executable
        return super().executable()

    def missing_reason(self, hw):
        return "No instalado (pulsa Instalar) o indica un servidor en MLC_URL"

    def placements(self, hw, spec):
        return ["gpu", "cpu"] if hw["has_gpu"] else ["cpu"]

    def command(self, spec, port):
        exe = self.executable()
        pre = [exe, "-m", "mlc_llm"] if exe == sys.executable else [exe]
        device = "cpu" if spec.get("placement") == "cpu" else "auto"
        return pre + ["serve", spec["path"], "--device", device, "--host", "127.0.0.1", "--port", str(port),
                      "--mode", "local", "--overrides", f"context_window_size={config.NUM_CTX}"]


class TensorRT(ServerEngine):
    id = "tensorrt"
    name = "TensorRT-LLM"
    formats = ("tensorrt", "hf")
    url_setting = "TRTLLM_URL"
    exe_names = ("trtllm-serve",)
    platforms = ("linux",)
    homepage = "https://nvidia.github.io/TensorRT-LLM/installation/linux.html"

    def available(self, hw):
        if self.external_url():
            return True, ""
        if not hw["nvidia"]:
            return False, "Necesita una GPU NVIDIA"
        if hw["os"] != "linux":
            return False, "Solo funciona en Linux o WSL2: arranca trtllm-serve allí y pon su URL en TRTLLM_URL"
        return super().available(hw)

    def missing_reason(self, hw):
        return "Instala TensorRT-LLM (pip install tensorrt-llm) o indica un servidor en TRTLLM_URL"

    def placements(self, hw, spec):
        return ["gpu"]

    def command(self, spec, port):
        return [self.executable(), spec["path"], "--host", "127.0.0.1", "--port", str(port),
                "--max_seq_len", str(config.NUM_CTX)]
