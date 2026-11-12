"""Herramientas que pueden usar los agentes (formato de tool calling de Ollama/OpenAI)."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from typing import Any, Awaitable, Callable

from . import config, memory, multimodal, workspace


def _schema(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": required}}}


S = {"type": "string"}

SCHEMAS: dict[str, dict] = {
    "list_files": _schema("list_files", "Lista los archivos del workspace (o de una subcarpeta).",
                          {"folder": {**S, "description": "Subcarpeta, vacío = raíz"}}, []),
    "read_file": _schema("read_file", "Lee un archivo de texto, PDF o DOCX del workspace (por partes si es largo).",
                         {"path": S, "offset": {"type": "integer", "description": "Carácter desde el que leer (0 = inicio)"}},
                         ["path"]),
    "write_file": _schema("write_file", "Crea o sobrescribe un archivo de texto en el workspace.",
                          {"path": S, "content": S}, ["path", "content"]),
    "search_workspace": _schema("search_workspace",
                                "Búsqueda semántica en los documentos indexados del workspace.",
                                {"query": S}, ["query"]),
    "remember": _schema("remember", "Guarda en la memoria a largo plazo un hecho o preferencia importante.",
                        {"fact": S}, ["fact"]),
    "recall": _schema("recall", "Busca en la memoria a largo plazo recuerdos relacionados.",
                      {"query": S}, ["query"]),
    "run_python": _schema("run_python",
                          "Ejecuta código Python en el workspace (timeout limitado) y devuelve stdout/stderr. "
                          "Úsalo para cálculos, análisis de datos o generar archivos.",
                          {"code": S}, ["code"]),
    "look_at_image": _schema("look_at_image", "Analiza una imagen del workspace con el modelo de visión.",
                             {"path": S, "question": S}, ["path", "question"]),
    "generate_image": _schema("generate_image",
                              "Genera una imagen con difusión local (ComfyUI) y la guarda en el workspace.",
                              {"prompt": {**S, "description": "Descripción en inglés, detallada"},
                               "filename": {**S, "description": "p. ej. imagenes/gato.png"}},
                              ["prompt", "filename"]),
}


async def _list_files(folder: str = "") -> str:
    items = workspace.tree(folder)
    if not items:
        return "(workspace vacío)"
    return "\n".join(f"{'📁' if i['dir'] else '📄'} {i['path']}" + ("" if i["dir"] else f" ({i['size']} B)")
                     for i in items[:300])


async def _read_file(path: str, offset: int = 0) -> str:
    size = config.TOOL_RESULT_CHARS
    text = await asyncio.to_thread(workspace.read_text, path, 2_000_000)
    offset = max(0, int(offset or 0))
    part = text[offset:offset + size]
    if offset + size < len(text):
        part += f"\n…[quedan {len(text) - offset - size} caracteres: llama a read_file con offset={offset + size}]"
    return part


async def _write_file(path: str, content: str) -> str:
    p = workspace.write_text(path, content)
    if workspace.is_indexable(p):
        try:
            await memory.index_document(p, content)
        except Exception:
            pass
    return f"Guardado {p} ({len(content)} caracteres)"


async def _search_workspace(query: str) -> str:
    hits = await memory.recall(query, limit=6, kind="documento", min_score=0.3)
    if not hits:
        return "Sin resultados en los documentos indexados."
    return "\n\n".join(f"[{h['path']} · {h['score']}]\n{h['text']}" for h in hits)


async def _remember(fact: str) -> str:
    r = await memory.remember(fact, kind="hecho", source="agente")
    return "Recuerdo actualizado" if r["updated_existing"] else "Recuerdo guardado"


async def _recall(query: str) -> str:
    hits = await memory.recall(query, limit=8, kind="hecho")
    return "\n".join(f"- {h['text']}" for h in hits) or "No recuerdo nada relacionado."


async def _run_python(code: str) -> str:
    if not config.ENABLE_CODE_EXEC:
        return "La ejecución de código está desactivada (ENABLE_CODE_EXEC=false)."
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code)
        script = f.name
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", script, cwd=str(config.WORKSPACE_DIR),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=config.CODE_EXEC_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        return f"Timeout tras {config.CODE_EXEC_TIMEOUT}s"
    finally:
        try:
            os.unlink(script)
        except OSError:
            pass
    res = out.decode(errors="replace")[-8000:]
    if err:
        res += "\n[stderr]\n" + err.decode(errors="replace")[-4000:]
    return res or "(sin salida)"


async def _look_at_image(path: str, question: str) -> str:
    data = await asyncio.to_thread(workspace.resolve(path).read_bytes)
    return await multimodal.describe_images([multimodal.b64(data)], question)


async def _generate_image(prompt: str, filename: str) -> str:
    img = await multimodal.generate_image(prompt)
    p = workspace.write_bytes(filename, img)
    return f"Imagen guardada en {p}. Muéstrala al usuario con ![imagen](/api/workspace/raw?path={p})"


IMPL: dict[str, Callable[..., Awaitable[str]]] = {
    "list_files": _list_files, "read_file": _read_file, "write_file": _write_file,
    "search_workspace": _search_workspace, "remember": _remember, "recall": _recall,
    "run_python": _run_python, "look_at_image": _look_at_image, "generate_image": _generate_image,
}


async def execute(name: str, args: dict | str) -> str:
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except json.JSONDecodeError:
            return f"Argumentos JSON inválidos para {name}"
    fn = IMPL.get(name)
    if not fn:
        return f"Herramienta desconocida: {name}"
    try:
        return await fn(**args)
    except TypeError as e:
        return f"Argumentos incorrectos para {name}: {e}"
    except Exception as e:  # el error vuelve al agente para que pueda corregirse
        return f"Error en {name}: {type(e).__name__}: {e}"
