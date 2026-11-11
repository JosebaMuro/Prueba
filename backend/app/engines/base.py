"""Piezas comunes de los motores.

- Engine: interfaz que implementa cada motor (disponibilidad, ubicaciones RAM/VRAM/NPU, chat en streaming).
- Plantillas de chat: los motores "de bajo nivel" (ONNX Runtime GenAI, OpenVINO GenAI, ExecuTorch, Genie)
  reciben el prompt ya formateado. Se usa la plantilla Jinja del propio modelo (tokenizer_config.json o
  chat_template.jinja), con las herramientas en el formato que el modelo aprendió; si no trae plantilla, ChatML.
- Herramientas por texto: si la plantilla no admite herramientas, se describen en el prompt de sistema y la
  llamada se recupera del texto (<tool_call>{...}</tool_call>, <|tool_call|>[...] o JSON suelto).
- thread_stream: convierte un generador bloqueante (que corre en un hilo) en un iterador asíncrono.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from .. import config

PLACEMENTS = {
    "auto": "Automático",
    "gpu": "GPU (VRAM)",
    "cpu": "CPU (RAM)",
    "mixed": "Mixto (capas en GPU, resto en RAM)",
    "npu": "NPU",
}


class EngineUnavailable(RuntimeError):
    pass


class Engine:
    id = ""
    name = ""
    formats: tuple[str, ...] = ()
    install: list[str] | None = None   # paquetes pip del "pack" del motor (None = no instalable desde el estudio)
    platforms = ("windows", "linux", "macos")
    homepage = ""

    def available(self, hw: dict) -> tuple[bool, str]:
        """(disponible, motivo si no lo está)."""
        return False, "No implementado"

    def placements(self, hw: dict, spec: dict) -> list[str]:
        """Ubicaciones que este motor admite para este modelo en este equipo (sin "auto")."""
        return ["cpu"]

    def capabilities(self, spec: dict) -> set[str]:
        return {"completion", "tools"}

    async def chat_events(self, spec: dict, messages: list[dict], tools: list[dict] | None,
                          options: dict | None) -> AsyncIterator[tuple[str, Any]]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def unload(self) -> None:
        return None

    def describe(self, hw: dict) -> dict:
        ok, why = self.available(hw)
        return {"id": self.id, "name": self.name, "available": ok, "reason": why, "formats": list(self.formats),
                "installable": bool(self.install) and not ok and hw["os"] in self.platforms,
                "homepage": self.homepage}


# ------------------------------------------------------------------ mensajes y plantillas

def plain_messages(messages: list[dict], native_tools: bool) -> list[dict]:
    """Mensajes en el formato de las plantillas de Hugging Face. Sin herramientas nativas, las llamadas
    y sus resultados se escriben como texto para que cualquier plantilla los acepte."""
    out = []
    for m in messages:
        role, content = m.get("role", "user"), m.get("content") or ""
        if role == "tool":
            if native_tools:
                out.append({"role": "tool", "content": content, "name": m.get("tool_name", "")})
            else:
                out.append({"role": "user", "content": f"<tool_response>\n{content}\n</tool_response>"})
            continue
        if role == "assistant" and m.get("tool_calls"):
            calls = [{"name": c["function"]["name"], "arguments": c["function"].get("arguments", {})}
                     for c in m["tool_calls"]]
            if native_tools:
                out.append({"role": "assistant", "content": content,
                            "tool_calls": [{"type": "function", "function": c} for c in calls]})
            else:
                text = "\n".join(f"<tool_call>\n{json.dumps(c, ensure_ascii=False)}\n</tool_call>" for c in calls)
                out.append({"role": "assistant", "content": (content + "\n" + text).strip()})
            continue
        out.append({"role": role, "content": content})
    # Algunas plantillas exigen alternancia estricta usuario/asistente: se fusionan mensajes seguidos
    merged: list[dict] = []
    for m in out:
        if merged and merged[-1]["role"] == m["role"] == "user":
            merged[-1] = {**merged[-1], "content": merged[-1]["content"] + "\n\n" + m["content"]}
        else:
            merged.append(m)
    return merged


def tools_system_prompt(tools: list[dict]) -> str:
    """Descripción de herramientas al estilo Hermes/Qwen (la entienden la mayoría de modelos recientes)."""
    defs = "\n".join(json.dumps(t, ensure_ascii=False) for t in tools)
    return ("\n\n# Herramientas\n\nPuedes llamar a una o varias funciones para responder. "
            "Firmas disponibles dentro de <tools></tools>:\n<tools>\n" + defs + "\n</tools>\n\n"
            "Para llamar a una función responde SOLO con un objeto JSON dentro de etiquetas <tool_call>:\n"
            "<tool_call>\n{\"name\": <nombre>, \"arguments\": <objeto JSON con los argumentos>}\n</tool_call>\n"
            "Si no necesitas ninguna herramienta, responde directamente.")


def with_tools_in_system(messages: list[dict], tools: list[dict] | None) -> list[dict]:
    if not tools:
        return messages
    extra = tools_system_prompt(tools)
    if messages and messages[0]["role"] == "system":
        return [{**messages[0], "content": messages[0]["content"] + extra}] + messages[1:]
    return [{"role": "system", "content": extra.strip()}] + messages


def find_template(model_dir: Path | None) -> tuple[str, dict]:
    """(plantilla Jinja, tokens especiales) del modelo, buscando en sus archivos de tokenizer."""
    if not model_dir or not model_dir.is_dir():
        return "", {}
    tpl, special = "", {}
    for name in ("chat_template.jinja", "chat_template.json", "tokenizer_config.json"):
        f = model_dir / name
        if not f.exists():
            continue
        try:
            if name.endswith(".jinja"):
                tpl = tpl or f.read_text(encoding="utf-8")
                continue
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        t = data.get("chat_template")
        if isinstance(t, list):  # varias plantillas con nombre: se usa la de herramientas si la hay
            named = {x.get("name"): x.get("template") for x in t if isinstance(x, dict)}
            t = named.get("tool_use") or named.get("default") or next(iter(named.values()), "")
        if t and not tpl:
            tpl = t
        for k in ("bos_token", "eos_token"):
            v = data.get(k)
            if isinstance(v, dict):
                v = v.get("content")
            if v and k not in special:
                special[k] = v
    return tpl, special


CHATML = ("{% for m in messages %}<|im_start|>{{ m.role }}\n{{ m.content }}<|im_end|>\n{% endfor %}"
          "{% if add_generation_prompt %}<|im_start|>assistant\n{% endif %}")


def render(template: str, messages: list[dict], tools: list[dict] | None, special: dict | None = None,
           add_generation_prompt: bool = True) -> tuple[str, bool]:
    """Aplica la plantilla. Devuelve (prompt, herramientas_nativas). Si la plantilla no menciona herramientas,
    se describen en el prompt de sistema."""
    from jinja2 import TemplateError
    from jinja2.sandbox import ImmutableSandboxedEnvironment

    template = template or CHATML
    native = bool(tools) and "tools" in template
    msgs = plain_messages(messages, native)
    if tools and not native:
        msgs = with_tools_in_system(msgs, tools)

    def raise_exception(msg):
        raise TemplateError(msg)

    env = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True, extensions=["jinja2.ext.loopcontrols"])
    env.filters["tojson"] = lambda v, indent=None, ensure_ascii=False, **_: json.dumps(
        v, ensure_ascii=ensure_ascii, indent=indent)
    env.globals.update(raise_exception=raise_exception,
                       strftime_now=lambda fmt: datetime.now().strftime(fmt))
    ctx = {"messages": msgs, "add_generation_prompt": add_generation_prompt,
           "enable_thinking": config.THINK, "bos_token": "", "eos_token": "", **(special or {})}
    if native:
        ctx["tools"] = tools
    try:
        return env.from_string(template).render(**ctx), native
    except TemplateError:
        if template is CHATML:
            raise
        # Plantilla incompatible con estos mensajes (p. ej. exige alternancia): ChatML como último recurso
        return render(CHATML, messages, tools, special, add_generation_prompt)


# ------------------------------------------------------------------ salida del modelo

_CALL_PATTERNS = [
    re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S),
    re.compile(r"<\|tool_call\|>\s*(.*?)\s*<\|/tool_call\|>", re.S),       # Phi-4
    re.compile(r"<\|python_tag\|>\s*(\{.*\})", re.S),                      # Llama 3.x
    re.compile(r"```(?:json|tool_call)?\s*(\{.*?\}|\[.*?\])\s*```", re.S),
]


def parse_tool_calls(text: str, tools: list[dict] | None) -> tuple[str, list[dict]]:
    """Extrae llamadas a herramientas escritas como texto. Devuelve (texto restante, tool_calls)."""
    if not tools:
        return text, []
    names = {t["function"]["name"] for t in tools}
    raw: list[str] = []
    rest = text
    for pat in _CALL_PATTERNS:
        found = pat.findall(text)
        if found:
            raw, rest = found, pat.sub("", text)
            break
    if not raw:
        bare = text.strip()
        if (bare.startswith("{") and bare.endswith("}")) or (bare.startswith("[") and bare.endswith("]")):
            raw, rest = [bare], ""
    calls = []
    for chunk in raw:
        try:
            data = json.loads(chunk)
        except ValueError:
            continue
        for j in data if isinstance(data, list) else [data]:
            if not isinstance(j, dict):
                continue
            name = j.get("name") or (j.get("function") or {}).get("name")
            args = j.get("arguments", j.get("parameters", j.get("args", {})))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            if name in names:
                calls.append({"function": {"name": name, "arguments": args if isinstance(args, dict) else {}}})
    return (rest.strip() if calls else text), calls


class ThinkFilter:
    """Quita los bloques <think>…</think> de la salida en streaming (Qwen3, DeepSeek-R1…)."""

    def __init__(self):
        self.buf, self.inside = "", False

    def feed(self, piece: str) -> str:
        self.buf += piece
        out = []
        while True:
            if self.inside:
                i = self.buf.find("</think>")
                if i < 0:
                    self.buf = self.buf[-8:]
                    return "".join(out)
                self.buf, self.inside = self.buf[i + 8:].lstrip("\n"), False
            else:
                i = self.buf.find("<think>")
                if i < 0:
                    # Retener un posible "<think" a medio llegar
                    keep = next((k for k in range(min(7, len(self.buf)), 0, -1) if "<think>".startswith(self.buf[-k:])), 0)
                    out.append(self.buf[:len(self.buf) - keep])
                    self.buf = self.buf[len(self.buf) - keep:]
                    return "".join(out)
                out.append(self.buf[:i])
                self.buf, self.inside = self.buf[i + 7:], True

    def flush(self) -> str:
        rest, self.buf = ("" if self.inside else self.buf), ""
        return rest


STOP_STRINGS = ("<|im_end|>", "<|eot_id|>", "<end_of_turn>", "<|end|>", "</s>", "<|endoftext|>")


async def text_events(pieces: AsyncIterator[str], tools: list[dict] | None) -> AsyncIterator[tuple[str, Any]]:
    """Convierte texto en streaming en los eventos de llm.chat_events: ("text", trozo)… y ("message", msg).
    Si el modelo empieza a escribir una llamada a herramienta, el texto deja de mostrarse."""
    think, full, shown = ThinkFilter(), [], 0
    async for raw in pieces:
        piece = think.feed(raw)
        if not piece:
            continue
        full.append(piece)
        text = "".join(full)
        for s in STOP_STRINGS:
            if s in text:
                full, text = [text.split(s)[0]], text.split(s)[0]
        if tools:
            head = text.lstrip()
            if (head[:1] and head[:1] in "{[") or "<tool_call>" in text or "<|tool_call|>" in text or "<|python_tag|>" in text \
                    or head.startswith("```"):
                continue  # parece una llamada: no se enseña
        if len(text) > shown:
            yield "text", text[shown:]
            shown = len(text)
    full.append(think.flush())
    text = "".join(full)
    for s in STOP_STRINGS:
        text = text.split(s)[0]
    rest, calls = parse_tool_calls(text, tools)
    msg: dict = {"role": "assistant", "content": "" if calls else text.strip()}
    if calls:
        msg["tool_calls"] = calls
    yield "message", msg


async def thread_stream(work: Callable[[Callable[[str], bool]], None]) -> AsyncIterator[str]:
    """Ejecuta work(emit) en un hilo y entrega lo que emita. emit() devuelve False si hay que parar
    (el cliente cerró la conexión)."""
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()
    done = object()

    def emit(piece: str) -> bool:
        if piece:
            loop.call_soon_threadsafe(q.put_nowait, piece)
        return not stop.is_set()

    def runner():
        try:
            work(emit)
        except BaseException as e:  # noqa: BLE001 - se re-lanza en el lado asíncrono
            loop.call_soon_threadsafe(q.put_nowait, e)
        finally:
            loop.call_soon_threadsafe(q.put_nowait, done)

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    try:
        while True:
            item = await q.get()
            if item is done:
                break
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        stop.set()
