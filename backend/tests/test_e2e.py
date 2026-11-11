"""Prueba de extremo a extremo con Ollama simulado.

Ejecutar:  python -m pytest backend/tests -q   (o directamente: python backend/tests/test_e2e.py)
"""
import base64
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def test_full_flow():
    port = _free_port()
    tmp = tempfile.mkdtemp()
    os.environ.update(OLLAMA_URL=f"http://127.0.0.1:{port}", DATA_DIR=f"{tmp}/data",
                      WORKSPACE_DIR=f"{tmp}/ws", QDRANT_URL="")
    import mock_ollama
    threading.Thread(target=uvicorn.run, args=(mock_ollama.app,),
                     kwargs={"port": port, "log_level": "error"}, daemon=True).start()
    time.sleep(1.0)

    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        h = c.get("/api/health").json()
        assert h["ollama"] and h["missing"] == [], h

        # Chat multi-agente: supervisor -> programador -> run_python + write_file
        events = []
        with c.stream("POST", "/api/chat", json={"message": "¿Cuánto es 6*7? Me llamo Joseba"}) as r:
            for line in r.iter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        types = [e["type"] for e in events]
        assert "agent" in types and "tool_call" in types and types.count("done") == 1, types
        assert types[-1] == "memory_saved", types  # la memoria se guarda después de liberar el chat
        py = next(e for e in events if e["type"] == "tool_result" and e["tool"] == "run_python")
        assert py["result"].strip() == "42"
        answer = "".join(e["text"] for e in events if e["type"] == "token")
        assert "42" in answer
        cid = events[0]["id"]
        assert len(c.get(f"/api/conversations/{cid}").json()) == 2

        # Workspace: el agente creó el archivo y quedó indexado
        assert c.get("/api/workspace/file", params={"path": "resultado.txt"}).json()["content"] == "6*7 = 42"
        assert c.get("/api/workspace/file", params={"path": "../secret"}).status_code == 400

        # Memoria automática + manual + deduplicación
        mems = c.get("/api/memory").json()
        assert any("Joseba" in m["text"] for m in mems), mems
        r = c.post("/api/memory", json={"text": "El usuario se llama Joseba y prefiere Python"}).json()
        assert r["updated_existing"]
        assert c.get("/api/memory/search", params={"q": "Joseba Python"}).json()

        # Imagen adjunta -> modelo de visión
        img = base64.b64encode(b"\x89PNG fake").decode()
        with c.stream("POST", "/api/chat", json={"message": "¿Qué hay?", "images": [img], "mode": "escritor",
                                                 "conversation_id": cid}) as r:
            evs = [json.loads(l[6:]) for l in r.iter_lines() if l.startswith("data: ")]
        assert any(e.get("tool") == "vision" for e in evs)

        # Streaming real: la respuesta llega en varios trozos y el estado del motor se informa
        assert len([e for e in events if e["type"] == "token"]) > 3
        assert h["loaded"] and h["engine_opts"]["num_ctx"] == 8192 and h["profiles"]

        # Mismas opciones en todas las llamadas (si num_ctx cambiara, Ollama recargaría el modelo)
        chats = [b for b in mock_ollama.CALLS if b.get("messages")]
        assert {b["options"]["num_ctx"] for b in chats} == {8192}, {b["options"]["num_ctx"] for b in chats}
        assert all(b.get("keep_alive") == "30m" for b in chats)
        assert all(b.get("think") is False for b in chats if b["model"].startswith("qwen3:")), "think debe ir desactivado"
        assert not any("think" in b for b in chats if "vl" in b["model"])  # modelo sin thinking: no se envía
        # La memoria va en el mensaje del usuario, no en el sistema: el prefijo se mantiene y se reutiliza la caché
        assert not any("memoria a largo plazo" in b["messages"][0]["content"] for b in chats if b["messages"][0]["role"] == "system")

        # Delegación con final=true: la respuesta del especialista se muestra tal cual, sin reescribir
        with c.stream("POST", "/api/chat", json={"message": "directo por favor"}) as r:
            evs = [json.loads(l[6:]) for l in r.iter_lines() if l.startswith("data: ")]
        answer = "".join(e["text"] for e in evs if e["type"] == "token")
        assert answer == "Respuesta directa", answer
        assert [e["type"] for e in evs].count("done") == 1

        # Llamada a herramienta escrita como texto: se rescata y se ejecuta
        with c.stream("POST", "/api/chat", json={"message": "rescate de herramientas"}) as r:
            evs = [json.loads(l[6:]) for l in r.iter_lines() if l.startswith("data: ")]
        assert any(e["type"] == "tool_call" and e["tool"] == "list_files" for e in evs), evs
        toks, out = [], ""
        for e in evs:
            if e["type"] == "reset":
                out = ""
            elif e["type"] == "token":
                out += e["text"]
        assert out == "Archivos revisados", out

        # Lectura por partes de archivos largos
        c.put("/api/workspace/file", json={"path": "largo.txt", "content": "x" * 20000})
        from app import tools as T
        import asyncio
        part = asyncio.run(T.execute("read_file", {"path": "largo.txt"}))
        assert "offset=8000" in part

        # Subida de documento + búsqueda semántica
        c.post("/api/workspace/upload", files=[("files", ("notas.md", b"El proyecto Faro usa Qdrant para memoria"))])
        res = c.get("/api/memory/search", params={"q": "proyecto Faro usa Qdrant"}).json()
        assert any(m.get("path") == "notas.md" for m in res), res
    print("OK: flujo completo verificado")


if __name__ == "__main__":
    test_full_flow()
