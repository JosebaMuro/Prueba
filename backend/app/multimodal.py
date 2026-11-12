"""Capacidades multimodales locales.

- Voz a texto: faster-whisper (CTranslate2), CPU o GPU.
- Texto a voz: Piper (voces ONNX, muy rápidas en CPU).
- Visión: modelos VLM servidos por Ollama (qwen2.5vl, llava, gemma3...).
- Generación de imágenes: ComfyUI vía su API HTTP (opcional).
"""
from __future__ import annotations

import asyncio
import base64
import io
import random
import uuid
import wave
from functools import lru_cache

import httpx

from . import config, llm

# ---------------------------------------------------------------- STT


_whisper_model = None
_whisper_device = ""


def _load_whisper(device: str):
    from faster_whisper import WhisperModel
    import os
    if device == "cuda":
        return WhisperModel(config.WHISPER_MODEL, device="cuda", compute_type="float16")
    threads = max(2, min(8, (os.cpu_count() or 4) - 1))
    return WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=threads)


def _whisper():
    global _whisper_model, _whisper_device
    if _whisper_model is None:
        try:
            import ctranslate2
            cuda = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            cuda = False
        _whisper_device = "cuda" if cuda else "cpu"
        try:
            _whisper_model = _load_whisper(_whisper_device)
        except Exception:
            _whisper_device, _whisper_model = "cpu", _load_whisper("cpu")
    return _whisper_model


def _run_whisper(audio: bytes) -> dict:
    segments, info = _whisper().transcribe(
        io.BytesIO(audio), vad_filter=True, beam_size=config.WHISPER_BEAM,
        language=config.WHISPER_LANGUAGE or None,
        condition_on_previous_text=False)  # evita bucles de texto repetido en audios largos
    return {"text": " ".join(s.text.strip() for s in segments).strip(), "language": info.language}


def _transcribe_sync(audio: bytes) -> dict:
    global _whisper_model, _whisper_device
    try:
        return _run_whisper(audio)
    except Exception:
        # Típico en Windows: hay GPU NVIDIA pero faltan las DLL de cuDNN/cuBLAS. Pasamos a CPU una vez.
        if _whisper_device != "cuda":
            raise
        _whisper_device, _whisper_model = "cpu", _load_whisper("cpu")
        return _run_whisper(audio)


async def transcribe(audio: bytes) -> dict:
    return await asyncio.to_thread(_transcribe_sync, audio)


# ---------------------------------------------------------------- TTS


@lru_cache(maxsize=1)
def _piper():
    from piper import PiperVoice
    if not config.PIPER_VOICE:
        raise RuntimeError("Configura PIPER_VOICE con la ruta a una voz .onnx de Piper")
    return PiperVoice.load(config.PIPER_VOICE)


def _speak_sync(text: str) -> bytes:
    voice = _piper()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        voice.synthesize_wav(text, wav)
    return buf.getvalue()


async def speak(text: str) -> bytes:
    return await asyncio.to_thread(_speak_sync, text)


# ---------------------------------------------------------------- Visión


async def describe_images(images_b64: list[str], question: str) -> str:
    msg = await llm.chat(config.MODEL_VISION, [{"role": "user", "content": question, "images": images_b64}])
    return msg.get("content", "")


# ---------------------------------------------------------------- Imagen (ComfyUI)

# Flujo mínimo txt2img compatible con cualquier checkpoint SD1.5/SDXL instalado en ComfyUI.
def _workflow(prompt: str, negative: str, ckpt: str, width: int, height: int, steps: int) -> dict:
    seed = random.randint(0, 2**31)
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["4", 1]}},
        "3": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": steps, "cfg": 6.5, "sampler_name": "euler", "scheduler": "normal",
            "denoise": 1.0, "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "studio", "images": ["8", 0]}},
    }


async def generate_image(prompt: str, negative: str = "blurry, low quality, deformed",
                         width: int = 1024, height: int = 1024, steps: int = 25) -> bytes:
    if not config.COMFYUI_URL:
        raise RuntimeError("ComfyUI no está configurado (COMFYUI_URL). Arráncalo con el perfil 'image'.")
    async with httpx.AsyncClient(base_url=config.COMFYUI_URL, timeout=600) as c:
        ckpts = (await c.get("/object_info/CheckpointLoaderSimple")).json()
        options = ckpts["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
        if not options:
            raise RuntimeError("No hay checkpoints en ComfyUI/models/checkpoints")
        wf = _workflow(prompt, negative, options[0], width, height, steps)
        pid = (await c.post("/prompt", json={"prompt": wf, "client_id": uuid.uuid4().hex})).json()["prompt_id"]
        for _ in range(1200):
            hist = (await c.get(f"/history/{pid}")).json()
            if pid in hist:
                img = hist[pid]["outputs"]["9"]["images"][0]
                r = await c.get("/view", params={"filename": img["filename"], "subfolder": img["subfolder"],
                                                 "type": img["type"]})
                return r.content
            await asyncio.sleep(0.5)
    raise TimeoutError("ComfyUI no terminó a tiempo")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def status() -> dict:
    def has(mod: str) -> bool:
        try:
            __import__(mod)
            return True
        except Exception:
            return False
    return {
        "stt": has("faster_whisper"),
        "tts": has("piper") and bool(config.PIPER_VOICE),
        "image": bool(config.COMFYUI_URL),
        "vision_model": config.MODEL_VISION,
        "stt_device": _whisper_device or "sin cargar",
    }


__all__ = ["transcribe", "speak", "describe_images", "generate_image", "status", "b64"]
