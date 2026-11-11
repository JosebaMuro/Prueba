"""Pruebas de la capa de motores (sin modelos reales salvo que existan, ver más abajo).

Ejecutar:  python -m pytest backend/tests/test_engines.py -q

Si existen las carpetas de modelos diminutos (pesos aleatorios) que crea tests/make_tiny_models.py, también
se prueba de verdad la generación con ONNX Runtime GenAI y OpenVINO GenAI, herramientas incluidas.
"""
import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp()
os.environ.setdefault("DATA_DIR", f"{TMP}/data")
os.environ.setdefault("WORKSPACE_DIR", f"{TMP}/ws")
TINY = Path(os.environ.get("TINY_MODELS", ROOT / "tests" / ".tiny"))

from app.engines import base, formats  # noqa: E402

TOOLS = [{"type": "function", "function": {"name": "run_python", "description": "Ejecuta Python",
                                           "parameters": {"type": "object", "properties": {"code": {"type": "string"}}}}}]


def test_parse_tool_calls_formats():
    for text in ('<tool_call>\n{"name": "run_python", "arguments": {"code": "1+1"}}\n</tool_call>',
                 '<|tool_call|>[{"name": "run_python", "arguments": {"code": "1+1"}}]<|/tool_call|>',
                 '{"name": "run_python", "parameters": {"code": "1+1"}}',
                 '```json\n{"name": "run_python", "arguments": "{\\"code\\": \\"1+1\\"}"}\n```'):
        rest, calls = base.parse_tool_calls(text, TOOLS)
        assert calls == [{"function": {"name": "run_python", "arguments": {"code": "1+1"}}}], (text, calls)
    rest, calls = base.parse_tool_calls('{"name": "otra", "arguments": {}}', TOOLS)
    assert not calls


def test_think_filter_streaming():
    f = base.ThinkFilter()
    out = "".join(f.feed(p) for p in ["<thi", "nk>pensando", " mucho</th", "ink>\nHola", " mundo"]) + f.flush()
    assert out == "Hola mundo", out


def test_render_templates():
    msgs = [{"role": "system", "content": "Eres útil"}, {"role": "user", "content": "Hola"},
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "run_python", "arguments": {"code": "1"}}}]},
            {"role": "tool", "content": "1", "tool_name": "run_python"}]
    # Sin plantilla: ChatML y herramientas descritas en el sistema
    text, native = base.render("", msgs, TOOLS)
    assert not native and "<tools>" in text and "<tool_response>" in text and text.endswith("<|im_start|>assistant\n")
    # Plantilla con herramientas nativas (estilo Qwen simplificado)
    tpl = ("{% if tools %}TOOLS:{{ tools | tojson }}\n{% endif %}{% for m in messages %}[{{ m.role }}]"
           "{% if m.tool_calls %}{{ m.tool_calls[0].function.name }}{% endif %}{{ m.content }}\n{% endfor %}"
           "{% if add_generation_prompt %}[assistant]{% endif %}")
    text, native = base.render(tpl, msgs, TOOLS)
    assert native and text.startswith("TOOLS:") and "[tool]1" in text and "[assistant]run_python" in text


def test_text_events_hides_tool_call():
    async def pieces():
        for p in ["<tool_call>", '{"name": "run_python", ', '"arguments": {"code": "6*7"}}', "</tool_call>", "<|im_end|>"]:
            yield p

    async def run():
        return [ev async for ev in base.text_events(pieces(), TOOLS)]

    evs = asyncio.run(run())
    assert [k for k, _ in evs] == ["message"], evs
    assert evs[-1][1]["tool_calls"][0]["function"]["arguments"] == {"code": "6*7"}


def test_formats_detect(tmp_path=None):
    d = Path(tempfile.mkdtemp())
    os.environ["MODELS_DIR"] = str(d)
    from app import config
    config.MODELS_DIR = d
    (d / "m-onnx").mkdir(); (d / "m-onnx" / "genai_config.json").write_text("{}")
    (d / "m-ov").mkdir(); (d / "m-ov" / "openvino_model.xml").write_text("")
    (d / "m.gguf").write_bytes(b"GGUF")
    (d / "m-et").mkdir(); (d / "m-et" / "model.pte").write_bytes(b"x"); (d / "m-et" / "tokenizer.json").write_text("{}")
    (d / "g").mkdir(); (d / "g" / "genie_config.json").write_text("{}")
    got = {m["ref"]: m["format"] for m in formats.list_local()}
    assert got == {"m-onnx": "onnx", "m-ov": "openvino", "m.gguf": "gguf", "m-et": "executorch", "g": "genie"}, got
    assert formats.detect("qwen3:8b")["format"] == "ollama"


def test_selection_and_placement():
    from app import engines
    hw_nv = {"os": "windows", "arch": "x64", "cpu": "AMD Ryzen", "cpu_vendor": "amd", "cores": 8, "ram_gb": 32,
             "gpus": [{"name": "RTX 4070", "vendor": "nvidia", "vram_gb": 12}], "npus": [], "nvidia": True,
             "best_vram_gb": 12, "has_gpu": True, "has_npu": False}
    e = engines.ENGINES["openvino"]
    assert engines._auto_placement(e, ["gpu", "cpu"], hw_nv, {"size_gb": 5}) == "gpu"
    assert engines._auto_placement(e, ["gpu", "cpu"], hw_nv, {"size_gb": 14}) == "cpu"   # no cabe en 12 GB
    hw_intel = {**hw_nv, "cpu_vendor": "intel", "cpu": "Intel Core Ultra", "nvidia": False, "best_vram_gb": 0,
                "gpus": [{"name": "Intel Arc", "vendor": "intel", "vram_gb": 0}]}
    assert engines.estimate("openvino", "gpu", hw_intel) > engines.estimate("llamacpp", "gpu", hw_intel)
    assert engines.estimate("tensorrt", "gpu", hw_nv) > engines.estimate("onnx", "gpu", hw_nv)
    assert engines.ollama.placement_options("cpu", 0) == {"num_gpu": 0}
    assert engines.ollama.placement_options("mixed", 20) == {"num_gpu": 20}
    assert engines.ollama.placement_options("auto", 0) == {}


# ------------------------------------------------------------------ llama-server simulado (API OpenAI)

def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def test_openai_server_engine_with_tools():
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse
    import uvicorn

    app = FastAPI()
    seen = {}

    @app.post("/v1/chat/completions")
    async def completions(req: Request):
        body = await req.json()
        seen["body"] = body
        last = body["messages"][-1]

        def sse():
            if last["role"] == "user" and body.get("tools"):
                d = {"tool_calls": [{"index": 0, "function": {"name": "run_python", "arguments": '{"code": '}}]}
                yield "data: " + json.dumps({"choices": [{"delta": d}]}) + "\n\n"
                d = {"tool_calls": [{"index": 0, "function": {"arguments": '"6*7"}'}}]}
                yield "data: " + json.dumps({"choices": [{"delta": d}]}) + "\n\n"
            else:
                for w in ["El ", "resultado ", "es 42"]:
                    yield "data: " + json.dumps({"choices": [{"delta": {"content": w}}]}) + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(sse(), media_type="text/event-stream")

    port = _free_port()
    threading.Thread(target=uvicorn.run, args=(app,), kwargs={"port": port, "log_level": "error"}, daemon=True).start()
    time.sleep(1)
    from app import config
    from app.engines.openai_server import LlamaServer
    config.LLAMA_SERVER_URL = f"http://127.0.0.1:{port}"
    e = LlamaServer()
    spec = {"ref": "x.gguf", "path": "/m/x.gguf", "format": "gguf", "placement": "gpu"}

    async def run(msgs, tools):
        return [ev async for ev in e.chat_events(spec, msgs, tools, None)]

    evs = asyncio.run(run([{"role": "user", "content": "6*7?"}], TOOLS))
    assert evs[-1][1]["tool_calls"][0]["function"] == {"name": "run_python", "arguments": {"code": "6*7"}}
    msgs = [{"role": "user", "content": "6*7?"}, evs[-1][1], {"role": "tool", "content": "42", "tool_name": "run_python"}]
    evs = asyncio.run(run(msgs, TOOLS))
    assert "".join(v for k, v in evs if k == "text") == "El resultado es 42"
    assert seen["body"]["messages"][-1]["role"] == "tool"
    config.LLAMA_SERVER_URL = ""


# ------------------------------------------------------------------ motores reales con modelos diminutos

def _real_engine(engine_id: str, model_dir: Path, placement: str = "cpu"):
    from app import engines
    e = engines.ENGINES[engine_id]
    info = formats.detect(str(model_dir))
    spec = {"ref": str(model_dir), **info, "placement": placement}

    async def run(msgs, tools):
        return [ev async for ev in e.chat_events(spec, msgs, tools, None)]

    t0 = time.time()
    evs = asyncio.run(run([{"role": "system", "content": "Eres útil"}, {"role": "user", "content": "hola"}], TOOLS))
    assert evs and evs[-1][0] == "message", evs
    msg = evs[-1][1]
    assert isinstance(msg.get("content"), str)
    # Segunda vuelta con historial de herramientas: la plantilla y el motor deben aceptarlo
    hist = [{"role": "user", "content": "hola"},
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "run_python", "arguments": {"code": "1"}}}]},
            {"role": "tool", "content": "1", "tool_name": "run_python"}]
    evs = asyncio.run(run(hist, TOOLS))
    assert evs[-1][0] == "message"
    asyncio.run(e.unload())
    return time.time() - t0, msg


def test_real_onnx_genai():
    d = TINY / "onnx"
    if not (d / "genai_config.json").exists():
        print("(sin modelo ONNX diminuto: se omite)"); return
    dt, msg = _real_engine("onnx", d)
    print(f"ONNX Runtime GenAI OK en {dt:.1f}s: {msg.get('content', '')[:60]!r}")


def test_real_openvino_genai():
    d = TINY / "openvino"
    if not (d / "openvino_model.xml").exists():
        print("(sin modelo OpenVINO diminuto: se omite)"); return
    dt, msg = _real_engine("openvino", d)
    print(f"OpenVINO GenAI OK en {dt:.1f}s: {msg.get('content', '')[:60]!r}")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("✓", name)
    print("OK: motores verificados")
