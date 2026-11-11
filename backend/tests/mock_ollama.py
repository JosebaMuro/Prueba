"""Ollama simulado para pruebas automáticas (no necesita GPU ni modelos).

- /api/embed: vectores deterministas por bolsa de palabras (textos parecidos → vectores parecidos).
- /api/chat: el "supervisor" delega en el programador, que escribe un archivo y ejecuta Python.
"""
import hashlib
import json
import math

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()
DIM = 64
CALLS: list[dict] = []  # peticiones de chat recibidas (para comprobar opciones en las pruebas)


def vec(text: str) -> list[float]:
    v = [0.0] * DIM
    for w in text.lower().split():
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1
    n = math.sqrt(sum(x * x for x in v)) or 1
    return [x / n for x in v]


@app.get("/api/version")
def version():
    return {"version": "mock"}


@app.post("/api/show")
def show(body: dict):
    name = body.get("model", "")
    caps = ["completion", "tools"] + (["thinking"] if name.startswith("qwen3:") else []) + \
        (["vision"] if "vl" in name else [])
    return {"capabilities": caps}


@app.get("/api/ps")
def ps():
    return {"models": [{"name": "qwen3:8b", "size": 6_000_000_000, "size_vram": 6_000_000_000, "context_length": 8192}]}


@app.get("/api/tags")
def tags():
    return {"models": [{"name": n} for n in ["qwen3:8b", "qwen3-vl:8b-instruct", "qwen3-embedding:0.6b"]]}


@app.post("/api/embed")
def embed(body: dict):
    inp = body["input"]
    return {"embeddings": [vec(t) for t in (inp if isinstance(inp, list) else [inp])]}


@app.post("/api/chat")
def chat(body: dict):
    CALLS.append(body)
    if not body.get("messages"):  # precarga (warmup)
        return {"message": {"role": "assistant", "content": ""}, "done": True}
    msg = _reply(body)["message"]
    if not body.get("stream"):
        return {"message": msg, "done": True}

    def gen():  # streaming como Ollama: texto en trozos y tool_calls en un chunk
        text = msg.get("content", "")
        for i in range(0, len(text), 7):
            yield json.dumps({"message": {"role": "assistant", "content": text[i:i + 7]}, "done": False}) + "\n"
        if msg.get("tool_calls"):
            yield json.dumps({"message": {"role": "assistant", "content": "", "tool_calls": msg["tool_calls"]}, "done": False}) + "\n"
        yield json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}) + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson")


def _reply(body: dict):
    msgs = body["messages"]
    system = msgs[0]["content"] if msgs and msgs[0]["role"] == "system" else ""
    tools = {t["function"]["name"] for t in body.get("tools") or []}
    last = msgs[-1]
    if "módulo de memoria" in system:
        return {"message": {"role": "assistant", "content": '{"hechos": ["El usuario se llama Joseba y prefiere Python"]}'}}
    if msgs and msgs[-1].get("images"):
        return {"message": {"role": "assistant", "content": "Una imagen de prueba con un cuadrado rojo."}}
    first_user = next((m["content"] for m in msgs if m["role"] == "user"), "")
    if "delegate" in tools and "directo" in first_user:  # delegación con respuesta final directa
        if last["role"] == "user":
            return {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {
                "name": "delegate", "arguments": {"agent": "escritor", "task": "Escribe un saludo", "final": True}}}]}}
        return {"message": {"role": "assistant", "content": "NO DEBERÍA REESCRIBIR"}}
    if "delegate" in tools and "rescate" in first_user:  # llamada a herramienta escrita como texto
        if last["role"] == "user":
            return {"message": {"role": "assistant", "content": '<tool_call>{"name": "list_files", "arguments": {}}</tool_call>'}}
        return {"message": {"role": "assistant", "content": "Archivos revisados"}}
    if "delegate" in tools:  # supervisor
        if last["role"] == "user":
            return {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {
                "name": "delegate", "arguments": {"agent": "programador", "task": "Calcula 6*7 y guarda el resultado en resultado.txt"}}}]}}
        return {"message": {"role": "assistant", "content": f"**Hecho.** El equipo respondió: {last['content']}"}}
    if "run_python" in tools:  # programador
        n_tool = sum(1 for m in msgs if m["role"] == "tool")
        if n_tool == 0:
            return {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {
                "name": "run_python", "arguments": {"code": "print(6*7)"}}}]}}
        if n_tool == 1:
            return {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {
                "name": "write_file", "arguments": {"path": "resultado.txt", "content": "6*7 = 42"}}}]}}
        return {"message": {"role": "assistant", "content": "El resultado es 42 y está en resultado.txt"}}
    return {"message": {"role": "assistant", "content": "Respuesta directa"}}
