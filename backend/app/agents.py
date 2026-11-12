"""Orquestación multi-agente (patrón supervisor, como LangGraph Supervisor / CrewAI hierarchical).

El Supervisor recibe la petición, decide si responde él mismo o delega subtareas en
agentes especialistas mediante la herramienta `delegate`. Cada especialista ejecuta su
propio bucle ReAct con sus herramientas y devuelve el resultado al Supervisor, que
sintetiza la respuesta final. Todo el proceso se emite como eventos para la interfaz.

Los agentes se pueden editar en caliente desde la interfaz (se guardan en data/agents.json).
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import AsyncIterator

from . import config, llm, memory, multimodal, tools, workspace

AGENTS_FILE = config.DATA_DIR / "agents.json"

DEFAULT_AGENTS: dict[str, dict] = {
    "investigador": {
        "name": "Investigador",
        "emoji": "🔎",
        "model": "",  # vacío = MODEL_CHAT
        "description": "Busca y sintetiza información de los documentos del workspace y de la memoria.",
        "system": "Eres un investigador meticuloso. Busca en el workspace y en la memoria antes de "
                  "responder, cita los archivos de los que sacas cada dato y señala lo que no sabes.",
        "tools": ["list_files", "read_file", "search_workspace", "recall"],
    },
    "programador": {
        "name": "Programador",
        "emoji": "💻",
        "model": "code",
        "description": "Escribe, ejecuta y depura código; analiza datos con Python; crea archivos.",
        "system": "Eres un ingeniero de software senior. Escribe código limpio, pruébalo con run_python "
                  "antes de darlo por bueno y guarda los entregables en el workspace con write_file.",
        "tools": ["list_files", "read_file", "write_file", "run_python"],
    },
    "escritor": {
        "name": "Escritor",
        "emoji": "✍️",
        "model": "",
        "description": "Redacta, resume, traduce y edita textos y documentos.",
        "system": "Eres un redactor experto. Escribes con claridad, estructura y el tono adecuado. "
                  "Si te piden un documento, guárdalo en el workspace en Markdown.",
        "tools": ["read_file", "write_file", "search_workspace"],
    },
    "artista": {
        "name": "Artista visual",
        "emoji": "🎨",
        "model": "",
        "description": "Analiza imágenes y genera imágenes nuevas con difusión local.",
        "system": "Eres director de arte. Para generar, escribe prompts en inglés muy descriptivos "
                  "(sujeto, estilo, luz, composición). Para analizar, usa look_at_image.",
        "tools": ["look_at_image", "generate_image", "list_files"],
    },
}

SUPERVISOR_SYSTEM = """Eres el Supervisor de un estudio de IA local con un equipo de agentes.
Responde siempre en el idioma del usuario.
- Si la petición es sencilla o conversacional, responde tú directamente, sin herramientas.
- Si requiere trabajo especializado, usa la herramienta `delegate` (puedes delegar varias veces,
  en secuencia, pasando a cada agente el contexto que necesite).
- Si un único especialista puede dar la respuesta completa al usuario, delega con final=true:
  su respuesta se mostrará tal cual y no tendrás que reescribirla (es mucho más rápido).
- Usa `remember` cuando el usuario comparta algo que convenga recordar a largo plazo.
- Al final, entrega una respuesta clara y completa al usuario, integrando el trabajo del equipo.

Equipo disponible:
{team}
"""


def load_agents() -> dict[str, dict]:
    if AGENTS_FILE.exists():
        return json.loads(AGENTS_FILE.read_text(encoding="utf-8"))
    return DEFAULT_AGENTS


def save_agents(agents: dict[str, dict]) -> None:
    AGENTS_FILE.write_text(json.dumps(agents, ensure_ascii=False, indent=2), encoding="utf-8")


def _model_for(agent: dict) -> str:
    m = agent.get("model") or ""
    if m == "code":
        return config.MODEL_CODE
    return m or config.MODEL_CHAT


def _delegate_schema(agents: dict[str, dict]) -> dict:
    return {"type": "function", "function": {
        "name": "delegate",
        "description": "Encarga una subtarea a un agente especialista y devuelve su resultado.",
        "parameters": {"type": "object", "properties": {
            "agent": {"type": "string", "enum": list(agents.keys())},
            "task": {"type": "string", "description": "Instrucciones completas y autocontenidas"},
            "final": {"type": "boolean", "description": "true si la respuesta del agente es ya la respuesta "
                                                         "final para el usuario y no hace falta reescribirla"}},
            "required": ["agent", "task"]}}}


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[recortado: {len(text) - limit} caracteres más. Pide una parte concreta si la necesitas]"


async def _react_loop(agent_id: str, agent: dict, messages: list[dict], tool_names: list[str],
                      extra_tools: list[dict] | None = None, depth: int = 0,
                      stream: bool = False) -> AsyncIterator[dict]:
    """Bucle ReAct genérico. Emite eventos y termina con {"type": "final", "content": ...}.

    Con stream=True (agente de primer nivel) el texto se envía a la interfaz según se genera:
    la respuesta final aparece al instante en vez de esperar al final de todo el proceso.
    Si resulta que el modelo estaba llamando a una herramienta, se emite "reset" para borrarlo.
    """
    schemas = [tools.SCHEMAS[t] for t in tool_names if t in tools.SCHEMAS] + (extra_tools or [])
    model = _model_for(agent)
    for _step in range(config.MAX_AGENT_STEPS):
        if stream:
            msg, shown = {}, False
            async for kind, val in llm.chat_events(model, messages, tools=schemas or None):
                if kind == "text":
                    shown = True
                    yield {"type": "token", "text": val, "agent": agent_id}
                else:
                    msg = val
            if shown and msg.get("tool_calls"):
                yield {"type": "reset"}
        else:
            msg = await llm.chat(model, messages, tools=schemas or None)
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            yield {"type": "final", "agent": agent_id, "content": msg.get("content", ""), "streamed": stream}
            return
        for call in calls:
            fn = call["function"]
            name, args = fn["name"], fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except json.JSONDecodeError:
                    args = {}
            yield {"type": "tool_call", "agent": agent_id, "tool": name, "args": args, "depth": depth}
            if name == "delegate" and depth == 0:
                result, direct = "", bool(args.get("final")) and len(calls) == 1
                async for ev in run_specialist(args.get("agent", ""), args.get("task", ""), depth=1,
                                               stream=direct):
                    if ev["type"] == "final":
                        result = ev["content"]
                    else:
                        yield ev
                if direct and result:
                    # El especialista ya ha escrito la respuesta para el usuario: no se reescribe
                    yield {"type": "final", "agent": args.get("agent", agent_id), "content": result,
                           "streamed": True}
                    return
            else:
                result = await tools.execute(name, args)
            yield {"type": "tool_result", "agent": agent_id, "tool": name, "result": result[:4000], "depth": depth}
            messages.append({"role": "tool", "content": _clip(result, config.TOOL_RESULT_CHARS), "tool_name": name})
    # Se alcanzó el límite de pasos: pedir una respuesta final sin herramientas
    final_msgs = messages + [{"role": "user", "content": "Resume tu trabajo y da la respuesta final ya."}]
    if stream:
        text = []
        async for piece in llm.chat_stream(model, final_msgs):
            text.append(piece)
            yield {"type": "token", "text": piece, "agent": agent_id}
        yield {"type": "final", "agent": agent_id, "content": "".join(text), "streamed": True}
    else:
        msg = await llm.chat(model, final_msgs)
        yield {"type": "final", "agent": agent_id, "content": msg.get("content", "")}


async def run_specialist(agent_id: str, task: str, depth: int = 0, history: list[dict] | None = None,
                         stream: bool = False, images: list[str] | None = None) -> AsyncIterator[dict]:
    agents = load_agents()
    agent = agents.get(agent_id)
    if not agent:
        yield {"type": "final", "agent": agent_id, "content": f"No existe el agente '{agent_id}'."}
        return
    yield {"type": "agent", "agent": agent_id, "name": agent["name"], "emoji": agent.get("emoji", "🤖"),
           "task": task, "depth": depth}
    messages = [{"role": "system", "content": agent["system"] + "\nResponde en el idioma de la tarea."}]
    messages += history or []
    user = {"role": "user", "content": task}
    if images:
        user["images"] = images
    messages.append(user)
    async for ev in _react_loop(agent_id, agent, messages, agent.get("tools", []), depth=depth, stream=stream):
        yield ev


async def _memory_context(text: str) -> str:
    try:
        facts = await memory.recall(text, limit=6, kind="hecho")
    except Exception:
        return ""
    if not facts:
        return ""
    return "Lo que recuerdas del usuario y sus proyectos (memoria a largo plazo):\n" + \
        "\n".join(f"- {f['text']}" for f in facts)


def _history(conversation_id: str) -> list[dict]:
    """Historial reciente dentro de un presupuesto de caracteres proporcional al contexto del modelo
    (≈ 3 caracteres por token y como mucho el 40 % del contexto), para no desbordarlo nunca."""
    budget = int(config.NUM_CTX * 3 * 0.4)
    out: list[dict] = []
    for m in reversed(memory.get_messages(conversation_id, limit=30)):
        if m["role"] not in ("user", "assistant"):
            continue
        content = m["content"]
        if len(content) > budget // 2:
            content = content[:budget // 2] + "…"
        if budget - len(content) < 0:
            break
        budget -= len(content)
        out.append({"role": m["role"], "content": content})
    return list(reversed(out))


async def run(conversation_id: str, user_text: str, images_b64: list[str], mode: str = "auto",
              auto_memory: bool = True) -> AsyncIterator[dict]:
    """Punto de entrada de un turno de chat. mode = 'auto' (supervisor) o el id de un agente."""
    agents = load_agents()
    history = _history(conversation_id)
    top = agents[mode] if mode != "auto" and mode in agents else {"name": "Supervisor", "model": ""}
    top_model = _model_for(top)

    # 1) Imágenes adjuntas: se guardan en el workspace. Si el modelo principal ve imágenes (p. ej.
    #    qwen3-vl), las recibe directamente; si no, el modelo de visión las describe primero.
    #    La búsqueda en memoria se hace a la vez (en paralelo).
    task = user_text
    saved: list[str] = []
    native_images: list[str] = []
    mem_task = asyncio.create_task(_memory_context(user_text))
    if images_b64:
        for i, img in enumerate(images_b64):
            path = workspace.write_bytes(f"subidas/{int(time.time() * 1000)}_{i}.png", base64.b64decode(img))
            saved.append(path)
        note = f"[El usuario adjuntó imágenes guardadas en el workspace: {', '.join(saved)}]"
        if "vision" in await llm.capabilities(top_model):
            native_images = images_b64
            task = f"{user_text}\n\n{note}"
        else:
            yield {"type": "status", "text": f"Analizando {len(images_b64)} imagen(es) con {config.MODEL_VISION}…"}
            try:
                desc = await multimodal.describe_images(
                    images_b64, f"Describe con detalle estas imágenes (texto incluido) para ayudar con: {user_text}")
            except Exception as e:
                desc = f"(no se pudieron analizar: {type(e).__name__}: {e})"
            task = f"{user_text}\n\n{note}\nDescripción del modelo de visión:\n{desc}"
            yield {"type": "tool_result", "agent": "vision", "tool": "vision", "result": desc, "depth": 0}

    memory.add_message(conversation_id, "user", user_text, images=saved)

    # 2) Memoria a largo plazo relevante. Va en el mensaje del usuario y no en el prompt de sistema:
    #    así el prefijo (sistema + historial) no cambia entre turnos y Ollama reutiliza su caché KV
    #    en vez de volver a procesar toda la conversación.
    mem_ctx = await mem_task
    if mem_ctx:
        yield {"type": "memory", "text": mem_ctx}
        task = f"{task}\n\n[{mem_ctx}]"

    # 3) Ejecución (la respuesta final llega en streaming)
    final, agent_used, streamed = "", mode, False
    try:
        if mode != "auto" and mode in agents:
            source = run_specialist(mode, task, history=history, stream=True, images=native_images or None)
        else:
            agent_used = "supervisor"
            team = "\n".join(f"- {k}: {a.get('emoji', '')} {a['name']} — {a['description']}" for k, a in agents.items())
            user = {"role": "user", "content": task}
            if native_images:
                user["images"] = native_images
            messages = [{"role": "system", "content": SUPERVISOR_SYSTEM.format(team=team)}, *history, user]
            source = _react_loop("supervisor", top, messages, ["remember", "recall", "list_files"],
                                 extra_tools=[_delegate_schema(agents)], stream=True)
        async for ev in source:
            if ev["type"] == "final":
                final, streamed = ev["content"], ev.get("streamed", False)
            else:
                yield ev
    except Exception as e:
        yield {"type": "error", "text": f"{type(e).__name__}: {e}"}
        return

    if not streamed:
        yield {"type": "reset"}
        for i in range(0, len(final), 40):
            yield {"type": "token", "text": final[i:i + 40]}
    memory.add_message(conversation_id, "assistant", final, agent=agent_used)

    # La interfaz ya puede liberar el chat: la memoria automática se procesa después
    yield {"type": "done"}

    # 4) Memoria automática (estilo Mem0)
    if auto_memory and final:
        stored = await memory.extract_and_store(user_text, final)
        if stored:
            yield {"type": "memory_saved", "facts": stored}
