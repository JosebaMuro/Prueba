"""Motores que se ejecutan dentro del propio estudio (bibliotecas de Python) o como programa de línea de órdenes:

- ONNX Runtime GenAI  (onnxruntime_genai): CPU, DirectML o WebGPU (cualquier GPU), CUDA, QNN (NPU Snapdragon).
- OpenVINO GenAI      (openvino_genai):    CPU, GPU y NPU de Intel. También lee .gguf directamente.
- LiteRT-LM           (litert_lm):         CPU y GPU (modelos .litertlm de Google: Gemma, Qwen, Phi…).
- ExecuTorch          (executorch):        modelos .pte exportados con PyTorch (la ubicación la fija la exportación).
- Qualcomm Genie      (genie-t2t-run):     NPU Hexagon de los Snapdragon X (SDK QAIRT de Qualcomm).

Se mantiene cargado un modelo por motor (el último usado); cambiar de modelo o de ubicación lo recarga.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

from .. import config
from .base import Engine, find_template, render, text_events, thread_stream, plain_messages, with_tools_in_system


def _has(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


class InProcess(Engine):
    module = ""

    def __init__(self):
        self.loaded = None      # objeto del motor
        self.key = None
        self.lock = asyncio.Lock()
        self.gen_lock = asyncio.Lock()

    def available(self, hw):
        if hw["os"] not in self.platforms:
            return False, "No disponible para este sistema"
        if not _has(self.module):
            return False, "No instalado: pulsa Instalar (descarga una vez)"
        return True, ""

    def load(self, spec: dict):
        raise NotImplementedError

    def close(self, obj) -> None:
        return None

    async def get(self, spec: dict):
        key = (spec["path"], spec.get("placement"))
        async with self.lock:
            if self.key != key:
                old, self.loaded, self.key = self.loaded, None, None
                if old is not None:
                    await asyncio.to_thread(self.close, old)
                self.loaded = await asyncio.to_thread(self.load, spec)
                self.key = key
            return self.loaded

    async def unload(self):
        async with self.lock:
            if self.loaded is not None:
                await asyncio.to_thread(self.close, self.loaded)
            self.loaded, self.key = None, None

    def prompt(self, spec: dict, messages, tools, fallback_template: str = "") -> str:
        d = Path(spec["path"])
        tpl, special = find_template(d if d.is_dir() else d.parent)
        text, _native = render(tpl or fallback_template, messages, tools, special)
        return text

    def generate(self, obj, spec: dict, prompt: str, emit) -> None:
        raise NotImplementedError

    async def chat_events(self, spec, messages, tools, options):
        obj = await self.get(spec)
        prompt = self.prompt(spec, messages, tools, self.engine_template(obj))
        async with self.gen_lock:  # una generación a la vez por modelo cargado
            async for ev in text_events(thread_stream(lambda emit: self.generate(obj, spec, prompt, emit)), tools):
                yield ev

    def engine_template(self, obj) -> str:
        return ""


# ------------------------------------------------------------------ ONNX Runtime GenAI

class OrtGenAI(InProcess):
    id = "onnx"
    name = "ONNX Runtime GenAI"
    formats = ("onnx",)
    module = "onnxruntime_genai"
    homepage = "https://onnxruntime.ai/docs/genai/"

    @property
    def install(self):
        import platform as _p
        if sys.platform == "win32":
            return ["onnxruntime-genai-directml"]   # DirectML: GPUs NVIDIA, AMD e Intel en Windows
        return ["onnxruntime-genai-cuda"] if shutil.which("nvidia-smi") and _p.machine() == "x86_64" \
            else ["onnxruntime-genai"]

    def _providers(self) -> set[str]:
        try:
            import onnxruntime_genai as og
            out = {"cpu"}
            for name, fn in (("dml", "is_dml_available"), ("cuda", "is_cuda_available"), ("qnn", "is_qnn_available"),
                             ("webgpu", "is_webgpu_available")):
                f = getattr(og, fn, None)
                if f and f():
                    out.add(name)
            return out
        except Exception:
            return {"cpu"}

    def placements(self, hw, spec):
        prov = self._providers()
        out = []
        if prov & {"dml", "cuda", "webgpu"} and hw["has_gpu"]:
            out.append("gpu")
        # QNN (NPU Hexagon) solo en Snapdragon: los paquetes de PC dicen tenerlo aunque no haya NPU
        if hw["cpu_vendor"] == "qualcomm" and ("qnn" in prov or "qnn" in (spec.get("providers") or [])):
            out.append("npu")
        return out + ["cpu"]

    def load(self, spec):
        import onnxruntime_genai as og
        cfg = og.Config(spec["path"])
        pl = spec.get("placement")
        cfg.clear_providers()
        if pl == "gpu":
            prov = self._providers()
            cfg.append_provider(next(p for p in ("cuda", "dml", "webgpu") if p in prov))
        elif pl == "npu":
            cfg.append_provider("qnn")
        model = og.Model(cfg)
        return {"model": model, "tok": og.Tokenizer(model)}

    def close(self, obj):
        obj.clear()

    def generate(self, obj, spec, prompt, emit):
        import onnxruntime_genai as og
        tok = obj["tok"]
        ids = tok.encode(prompt)
        params = og.GeneratorParams(obj["model"])
        opts = {"max_length": min(len(ids) + config.MAX_NEW_TOKENS, max(config.NUM_CTX, len(ids) + 256))}
        if config.TEMPERATURE > 0:
            opts.update(do_sample=True, temperature=config.TEMPERATURE, top_p=0.95, top_k=40)
        params.set_search_options(**opts)
        gen = og.Generator(obj["model"], params)
        gen.append_tokens(ids)
        stream = tok.create_stream()
        while not gen.is_done():
            gen.generate_next_token()
            if not emit(stream.decode(gen.get_next_tokens()[0])):
                break
        del gen


# ------------------------------------------------------------------ OpenVINO GenAI

class OpenVINO(InProcess):
    id = "openvino"
    name = "OpenVINO GenAI"
    formats = ("openvino", "gguf")
    module = "openvino_genai"
    install = ["openvino-genai"]
    homepage = "https://openvinotoolkit.github.io/openvino.genai/"

    def devices(self) -> list[str]:
        try:
            import openvino
            return list(openvino.Core().available_devices)
        except Exception:
            return ["CPU"]

    def placements(self, hw, spec):
        devs = self.devices()
        out = []
        if any(d.startswith("GPU") for d in devs):
            out.append("gpu")
        if "NPU" in devs and spec.get("format") == "openvino":
            out.append("npu")
        return out + ["cpu"]

    def load(self, spec):
        import openvino_genai as ov
        device = {"gpu": "GPU", "npu": "NPU"}.get(spec.get("placement"), "CPU")
        kw = {"CACHE_DIR": str(config.DATA_DIR / "ov-cache")}
        if device == "NPU":
            kw.update(MAX_PROMPT_LEN=min(config.NUM_CTX, 4096), MIN_RESPONSE_LEN=512)
        return ov.LLMPipeline(spec["path"], device, **kw)

    def engine_template(self, pipe) -> str:
        try:
            return pipe.get_tokenizer().chat_template or ""
        except Exception:
            return ""

    def generate(self, pipe, spec, prompt, emit):
        import openvino_genai as ov
        cfg = pipe.get_generation_config()
        cfg.max_new_tokens = config.MAX_NEW_TOKENS
        if hasattr(cfg, "apply_chat_template"):
            cfg.apply_chat_template = False  # el prompt ya va formateado
        if config.TEMPERATURE > 0:
            cfg.do_sample, cfg.temperature, cfg.top_p, cfg.top_k = True, config.TEMPERATURE, 0.95, 40
        else:
            cfg.do_sample = False
        status = getattr(ov, "StreamingStatus", None)

        def streamer(piece: str):
            go = emit(piece)
            if status is not None:
                return status.RUNNING if go else status.CANCEL
            return not go

        pipe.generate(prompt, cfg, streamer)


# ------------------------------------------------------------------ LiteRT-LM

class LiteRT(InProcess):
    id = "litert"
    name = "LiteRT-LM"
    formats = ("litert",)
    module = "litert_lm"
    install = ["litert-lm-api"]
    homepage = "https://ai.google.dev/edge/litert-lm"

    def placements(self, hw, spec):
        return (["gpu"] if hw["has_gpu"] else []) + (["npu"] if hw["has_npu"] else []) + ["cpu"]

    def load(self, spec):
        import litert_lm as lm
        backend = {"gpu": lm.Backend.GPU(), "npu": lm.Backend.NPU()}.get(spec.get("placement"), lm.Backend.CPU())
        cache = config.DATA_DIR / "litert-cache"
        cache.mkdir(parents=True, exist_ok=True)
        return lm.Engine(spec["path"], backend=backend, max_num_tokens=config.NUM_CTX, cache_dir=str(cache))

    def close(self, eng):
        eng.close()

    async def chat_events(self, spec, messages, tools, options):
        eng = await self.get(spec)
        # LiteRT-LM aplica la plantilla del modelo: se le pasan los mensajes, con las herramientas en el sistema
        msgs = with_tools_in_system(plain_messages(messages, False), tools)
        system = msgs[0]["content"] if msgs and msgs[0]["role"] == "system" else None
        history = [m for m in msgs if m["role"] != "system"]
        last = history.pop() if history and history[-1]["role"] == "user" else {"role": "user", "content": ""}

        def work(emit):
            conv = eng.create_conversation(messages=history or None, system_message=system,
                                           automatic_tool_calling=False, max_output_tokens=config.MAX_NEW_TOKENS)
            try:
                for m in conv.send_message_async(last["content"]):
                    data = m.to_json() if hasattr(m, "to_json") else m
                    parts = data.get("content", [])
                    parts = parts if isinstance(parts, list) else [parts]
                    text = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in parts)
                    if not emit(text):
                        conv.cancel_process()
                        break
            finally:
                conv.close()

        async with self.gen_lock:
            async for ev in text_events(thread_stream(work), tools):
                yield ev


# ------------------------------------------------------------------ ExecuTorch

class ExecuTorch(InProcess):
    id = "executorch"
    name = "ExecuTorch"
    formats = ("executorch",)
    module = "executorch"
    install = ["executorch"]
    homepage = "https://docs.pytorch.org/executorch/stable/llm/getting-started.html"

    def placements(self, hw, spec):
        return ["cpu"]  # el backend (XNNPACK = CPU, Vulkan/CUDA = GPU, QNN = NPU) lo decide la exportación del .pte

    def load(self, spec):
        from executorch.extension.llm.runner import TextLLMRunner
        if not spec.get("tokenizer"):
            raise RuntimeError("Falta el tokenizer (tokenizer.json o tokenizer.model) junto al archivo .pte")
        return TextLLMRunner(spec["path"], spec["tokenizer"])

    def generate(self, runner, spec, prompt, emit):
        from executorch.extension.llm.runner import GenerationConfig
        runner.reset()
        cfg = GenerationConfig(echo=False, max_new_tokens=config.MAX_NEW_TOKENS, seq_len=config.NUM_CTX,
                               temperature=config.TEMPERATURE)

        def on_token(piece: str):
            if not emit(piece):
                runner.stop()

        runner.generate(prompt, cfg, on_token)


# ------------------------------------------------------------------ Qualcomm Genie

class Genie(InProcess):
    id = "genie"
    name = "Qualcomm Genie (QNN)"
    formats = ("genie",)
    platforms = ("windows", "linux")
    homepage = "https://github.com/quic/ai-hub-apps/tree/main/tutorials/llm_on_genie"

    def executable(self) -> str:
        names = ("genie-t2t-run.exe", "genie-t2t-run")
        dirs = [Path(config.GENIE_BIN)] if config.GENIE_BIN else []
        dirs += [config.ENGINE_PACKS_DIR / "genie"]
        if sys.platform == "win32":
            dirs += sorted(Path(r"C:\Qualcomm\AIStack\QAIRT").glob("*/bin/aarch64-windows-msvc"), reverse=True)
        for d in dirs:
            for n in names:
                if (d / n).exists():
                    return str(d / n)
        return next((shutil.which(n) for n in names if shutil.which(n)), "") or ""

    def available(self, hw):
        if hw["cpu_vendor"] != "qualcomm":
            return False, "Necesita un procesador Snapdragon (NPU Hexagon)"
        if not self.executable():
            return False, ("Instala el SDK QAIRT de Qualcomm (genie-t2t-run) o indica su carpeta en GENIE_BIN; "
                           "Qualcomm no permite redistribuirlo")
        return True, ""

    def placements(self, hw, spec):
        return ["npu"]

    def load(self, spec):
        return {"exe": self.executable()}

    async def chat_events(self, spec, messages, tools, options):
        info = await self.get(spec)
        prompt = self.prompt(spec, messages, tools)
        cfg = spec.get("config") or str(Path(spec["path"]) / "genie_config.json")

        def work(emit):
            flags = 0x08000000 if sys.platform == "win32" else 0
            p = subprocess.Popen([info["exe"], "-c", cfg, "-p", prompt], cwd=str(Path(cfg).parent),
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags,
                                 text=True, encoding="utf-8", errors="replace", bufsize=1)
            buf, started = "", False
            try:
                while True:
                    ch = p.stdout.read(1)
                    if not ch:
                        break
                    buf += ch
                    if not started:
                        i = buf.find("[BEGIN]:")
                        if i >= 0:
                            started, buf = True, buf[i + 8:].lstrip()
                        continue
                    if "[END]" in buf:
                        emit(buf.split("[END]")[0])
                        break
                    if len(buf) > 8 and "[" not in buf[-6:]:
                        if not emit(buf):
                            break
                        buf = ""
            finally:
                if p.poll() is None:
                    p.kill()
            if not started:
                raise RuntimeError("Genie no generó respuesta:\n" + buf[-1500:])

        async with self.gen_lock:
            async for ev in text_events(thread_stream(work), tools):
                yield ev
