"""Studio IA Local: API HTTP + interfaz web."""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agents, config, llm, memory, multimodal, workspace

VERSION = "1.2.0"

# Modelos recomendados por perfil de hardware (todos en la librería de Ollama, comprobados en nov. 2026).
# Menos modelos distintos = menos cambios de modelo en la GPU = respuestas mucho más rápidas.
RECOMMENDED = {
    "ligero (8 GB RAM, sin GPU) · un solo modelo para todo": {
        "MODEL_CHAT": "qwen3-vl:4b-instruct", "MODEL_CODE": "qwen3-vl:4b-instruct",
        "MODEL_VISION": "qwen3-vl:4b-instruct", "MODEL_EMBED": "qwen3-embedding:0.6b", "NUM_CTX": "8192"},
    "equilibrado (16 GB RAM o GPU de 8 GB)": {
        "MODEL_CHAT": "qwen3:8b", "MODEL_CODE": "qwen3:8b",
        "MODEL_VISION": "qwen3-vl:8b-instruct", "MODEL_EMBED": "qwen3-embedding:0.6b", "NUM_CTX": "8192"},
    "potente (GPU de 24 GB)": {
        "MODEL_CHAT": "qwen3:30b-a3b-instruct-2507-q4_K_M", "MODEL_CODE": "qwen3:30b-a3b-instruct-2507-q4_K_M",
        "MODEL_VISION": "qwen3-vl:8b-instruct", "MODEL_EMBED": "qwen3-embedding:0.6b", "NUM_CTX": "16384"},
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    memory.init_db()
    warm = asyncio.create_task(llm.warmup()) if config.WARMUP else None
    yield
    if warm and not warm.done():
        warm.cancel()


app = FastAPI(title="Studio IA Local", version=VERSION, lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def auth(request: Request, call_next):
    """Protección opcional por token para el acceso desde la red local (app Android)."""
    if config.ACCESS_TOKEN and request.url.path.startswith("/api") and request.url.path != "/api/ping":
        token = (request.headers.get("authorization", "").removeprefix("Bearer ").strip()
                 or request.query_params.get("token") or request.cookies.get("studio_token") or "")
        if not secrets.compare_digest(token.strip().upper(), config.ACCESS_TOKEN.strip().upper()):
            return JSONResponse({"detail": "Token de acceso incorrecto"}, status_code=401)
    return await call_next(request)


@app.exception_handler(workspace.WorkspaceError)
async def ws_error(_req, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


# ------------------------------------------------------------------ estado y modelos

@app.get("/api/ping")
async def ping():
    return {"app": "studio-ia-local", "version": VERSION, "auth": bool(config.ACCESS_TOKEN)}


@app.get("/api/health")
async def health():
    models = await llm.list_models()
    needed = {config.MODEL_CHAT, config.MODEL_CODE, config.MODEL_VISION, config.MODEL_EMBED}

    def installed(m: str) -> bool:
        return any(x == m or x == f"{m}:latest" for x in models)

    return {
        "version": VERSION,
        "ollama": bool(models) or await _ollama_up(),
        "models": models,
        "config": {"chat": config.MODEL_CHAT, "code": config.MODEL_CODE,
                   "vision": config.MODEL_VISION, "embed": config.MODEL_EMBED},
        "missing": sorted(m for m in needed if not installed(m)),
        "loaded": await llm.running_models(),
        "engine_opts": {"num_ctx": config.NUM_CTX, "keep_alive": config.KEEP_ALIVE, "think": config.THINK},
        "multimodal": multimodal.status(),
        "recommended": {k: sorted(set(v[m] for m in ("MODEL_CHAT", "MODEL_CODE", "MODEL_VISION", "MODEL_EMBED")))
                        for k, v in RECOMMENDED.items()},
        "profiles": RECOMMENDED,
    }


async def _ollama_up() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            return (await c.get(f"{config.OLLAMA_URL}/api/version")).status_code == 200
    except httpx.HTTPError:
        return False


class PullReq(BaseModel):
    model: str


@app.post("/api/models/pull")
async def pull_model(req: PullReq):
    """Descarga un modelo en Ollama y transmite el progreso (NDJSON)."""
    async def gen():
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("POST", f"{config.OLLAMA_URL}/api/pull", json={"model": req.model}) as r:
                async for line in r.aiter_lines():
                    if line:
                        yield line + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson")


# ------------------------------------------------------------------ conversaciones y chat

@app.get("/api/conversations")
async def conversations():
    return memory.list_conversations()


@app.post("/api/conversations")
async def create_conversation():
    return {"id": memory.new_conversation()}


@app.get("/api/conversations/{cid}")
async def conversation(cid: str):
    return memory.get_messages(cid)


class Rename(BaseModel):
    title: str


@app.patch("/api/conversations/{cid}")
async def rename(cid: str, body: Rename):
    memory.rename_conversation(cid, body.title)
    return {"ok": True}


@app.delete("/api/conversations/{cid}")
async def delete_conversation(cid: str):
    memory.delete_conversation(cid)
    return {"ok": True}


class ChatReq(BaseModel):
    conversation_id: str | None = None
    message: str
    images: list[str] = []      # base64 sin cabecera data:
    mode: str = "auto"          # "auto" (supervisor) o id de agente
    auto_memory: bool = True


@app.post("/api/chat")
async def chat(req: ChatReq):
    cid = req.conversation_id or memory.new_conversation(req.message[:60] or "Imagen")
    if not memory.get_messages(cid, limit=1):
        memory.rename_conversation(cid, req.message[:60] or "Imagen")

    async def sse():
        yield f"data: {json.dumps({'type': 'conversation', 'id': cid})}\n\n"
        async for ev in agents.run(cid, req.message, req.images, req.mode, req.auto_memory):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ agentes

@app.get("/api/agents")
async def get_agents():
    return agents.load_agents()


@app.put("/api/agents")
async def put_agents(body: dict):
    for k, a in body.items():
        if not {"name", "system", "tools", "description"} <= a.keys():
            raise HTTPException(400, f"Agente '{k}' incompleto")
    agents.save_agents(body)
    return {"ok": True}


@app.post("/api/agents/reset")
async def reset_agents():
    agents.save_agents(agents.DEFAULT_AGENTS)
    return agents.DEFAULT_AGENTS


@app.get("/api/tools")
async def list_tools():
    return {k: v["function"]["description"] for k, v in agents.tools.SCHEMAS.items()}


# ------------------------------------------------------------------ memoria

class MemoryReq(BaseModel):
    text: str


@app.get("/api/memory")
async def get_memory(kind: str | None = "hecho"):
    return await memory.list_memories(kind)


@app.get("/api/memory/search")
async def search_memory(q: str):
    return await memory.recall(q, limit=10, min_score=0.2)


@app.post("/api/memory")
async def add_memory(req: MemoryReq):
    return await memory.remember(req.text, source="usuario")


@app.delete("/api/memory/{mid}")
async def delete_memory(mid: str):
    await memory.forget(mid)
    return {"ok": True}


# ------------------------------------------------------------------ workspace

@app.get("/api/workspace")
async def ws_tree():
    return workspace.tree()


@app.get("/api/workspace/file")
async def ws_read(path: str):
    return {"path": path, "content": workspace.read_text(path)}


@app.get("/api/workspace/raw")
async def ws_raw(path: str):
    p = workspace.resolve(path)
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)


class WriteReq(BaseModel):
    path: str
    content: str


@app.put("/api/workspace/file")
async def ws_write(req: WriteReq):
    p = workspace.write_text(req.path, req.content)
    indexed = await _try_index(p)
    return {"path": p, "indexed": indexed}


@app.post("/api/workspace/upload")
async def ws_upload(files: list[UploadFile] = File(...), folder: str = ""):
    out = []
    for f in files:
        p = workspace.write_bytes(f"{folder.strip('/')}/{f.filename}" if folder else f.filename, await f.read())
        out.append({"path": p, "indexed": await _try_index(p)})
    return out


@app.delete("/api/workspace")
async def ws_delete(path: str):
    workspace.delete(path)
    try:
        await memory.forget_source(f"workspace:{path}")
    except Exception:
        pass
    return {"ok": True}


@app.post("/api/workspace/reindex")
async def ws_reindex():
    files = [i["path"] for i in workspace.tree() if not i["dir"] and workspace.is_indexable(i["path"])]
    sem = asyncio.Semaphore(4)  # varios archivos a la vez sin saturar Ollama

    async def one(path: str) -> int:
        async with sem:
            return await _try_index(path)
    return {"chunks": sum(await asyncio.gather(*(one(p) for p in files)))}


async def _try_index(path: str) -> int:
    if not workspace.is_indexable(path):
        return 0
    try:
        text = await asyncio.to_thread(workspace.read_text, path, 500_000)  # PDF/DOCX sin bloquear el servidor
        return await memory.index_document(path, text)
    except Exception:
        return 0


# ------------------------------------------------------------------ multimodal

@app.post("/api/stt")
async def stt(audio: UploadFile = File(...)):
    try:
        return await multimodal.transcribe(await audio.read())
    except ImportError:
        raise HTTPException(501, "faster-whisper no está instalado")


class TTSReq(BaseModel):
    text: str


@app.post("/api/tts")
async def tts(req: TTSReq):
    try:
        return Response(await multimodal.speak(req.text[:4000]), media_type="audio/wav")
    except (ImportError, RuntimeError) as e:
        raise HTTPException(501, str(e))


class ImageReq(BaseModel):
    prompt: str
    width: int = 1024
    height: int = 1024


@app.post("/api/image")
async def image(req: ImageReq):
    try:
        data = await multimodal.generate_image(req.prompt, width=req.width, height=req.height)
    except RuntimeError as e:
        raise HTTPException(501, str(e))
    p = workspace.write_bytes(f"imagenes/{int(time.time())}.png", data)
    return {"path": p, "url": f"/api/workspace/raw?path={p}"}


# ------------------------------------------------------------------ interfaz

if config.FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=config.FRONTEND_DIR, html=True), name="ui")


def main():
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info")


if __name__ == "__main__":
    main()
