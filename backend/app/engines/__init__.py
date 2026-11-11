"""Motores de inferencia: registro, selección automática, ubicación (RAM/VRAM/NPU), pruebas de velocidad
e instalación bajo demanda.

Cómo se elige el motor de un modelo (ENGINE=auto o "Auto" en el panel):
 1. El formato del modelo decide qué motores pueden cargarlo (ver formats.py).
 2. De esos, solo cuentan los disponibles en este equipo (instalados y con el hardware que necesitan).
 3. Si ya hay pruebas de velocidad (⚡ Medir) de ese modelo, gana la combinación motor + ubicación más rápida.
 4. Si no, se usa una estimación por hardware: TensorRT-LLM en NVIDIA, OpenVINO en Intel, DirectML en
    otras GPU de Windows, Genie en la NPU de Snapdragon, llama.cpp como opción universal…
Cómo se elige la ubicación (PLACEMENT=auto): VRAM si el modelo cabe en la GPU, si no mixto o RAM.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import time
from typing import AsyncIterator

from .. import config
from . import formats, hardware
from .base import PLACEMENTS, Engine, EngineUnavailable
from .native import ExecuTorch, Genie, LiteRT, OpenVINO, OrtGenAI
from .ollama import Ollama
from .openai_server import MLC, LlamaServer, TensorRT

PACKS_SITE = config.ENGINE_PACKS_DIR / "site-packages"
if str(PACKS_SITE) not in sys.path:
    sys.path.append(str(PACKS_SITE))  # al final: nunca tapa las bibliotecas que trae el estudio

OLLAMA = Ollama()
# Mismo orden que la lista de motores de la interfaz
ENGINES: dict[str, Engine] = {e.id: e for e in (
    OrtGenAI(), OpenVINO(), LiteRT(), ExecuTorch(), MLC(), TensorRT(), Genie(), LlamaServer(), OLLAMA)}

SETTINGS_FILE = config.DATA_DIR / "engines.json"
ROLES = ("chat", "code", "vision", "embed")


# ------------------------------------------------------------------ ajustes guardados

def load_settings() -> dict:
    try:
        s = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    s.setdefault("models", {})   # ref -> {"engine": "auto", "placement": "auto", "gpu_layers": 0}
    s.setdefault("roles", {})    # chat/code/vision/embed -> ref (sustituye a MODEL_* de studio.env)
    s.setdefault("bench", {})    # ref -> {"motor|ubicación": {"tps": tokens/s, "ttft": s, "at": fecha}}
    return s


def save_settings(s: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")


def apply_roles(s: dict | None = None) -> None:
    """Los modelos elegidos en el panel tienen prioridad sobre studio.env."""
    s = s or load_settings()
    for role in ROLES:
        ref = (s["roles"].get(role) or "").strip()
        if ref:
            setattr(config, f"MODEL_{role.upper()}", ref)


def model_setting(ref: str, s: dict | None = None) -> dict:
    s = s or load_settings()
    m = s["models"].get(ref, {})
    return {"engine": m.get("engine") or config.ENGINE, "placement": m.get("placement") or config.PLACEMENT,
            "gpu_layers": int(m.get("gpu_layers") or config.GPU_LAYERS or 0)}


# ------------------------------------------------------------------ selección

_SCORE = {"tensorrt": 95, "genie": 85, "ollama": 75, "llamacpp": 74, "mlc": 70, "onnx": 66, "openvino": 66,
          "litert": 62, "executorch": 50}
_PLACE = {"gpu": 30, "npu": 22, "mixed": 12, "cpu": 0, "auto": 20}


def estimate(eid: str, placement: str, hw: dict) -> int:
    """Puntuación aproximada de rendimiento (sin medir). Más alto = más rápido esperado."""
    s = _SCORE.get(eid, 40) + _PLACE.get(placement, 0)
    intel = hw["cpu_vendor"] == "intel" or any(g["vendor"] == "intel" for g in hw["gpus"])
    if eid == "openvino" and intel and not hw["nvidia"]:
        s += 18                      # OpenVINO está optimizado para CPU, iGPU/Arc y NPU de Intel
    if eid == "onnx" and placement == "gpu" and hw["os"] == "windows" and not hw["nvidia"]:
        s += 12                      # DirectML: buena opción en GPUs AMD/Intel con Windows
    if eid in ("ollama", "llamacpp") and placement == "cpu" and not intel:
        s += 6                       # llama.cpp es de lo más rápido en CPU AMD/ARM
    if eid == "onnx" and placement == "npu" and hw["cpu_vendor"] == "qualcomm":
        s += 10
    return s


def _auto_placement(engine: Engine, places: list[str], hw: dict, info: dict) -> str:
    if engine.id == "ollama":
        return "auto"                # Ollama ya reparte capas entre VRAM y RAM según quepa
    size = info.get("size_gb") or 0
    for p in places:
        if p == "gpu":
            vram = hw["best_vram_gb"]
            if vram and size and size * 1.15 + 0.8 > vram:
                continue             # no cabe en la VRAM
            if not vram and size and size * 1.3 > hw["ram_gb"] * 0.6:
                continue             # GPU integrada: comparte la RAM
            return p
        if p == "mixed":
            continue                 # mixto solo si se pide (hay que decir cuántas capas)
        return p
    return "cpu"


def candidates(ref: str, hw: dict | None = None, info: dict | None = None) -> list[tuple[Engine, list[str]]]:
    hw = hw or hardware.detect()
    info = info or formats.detect(ref)
    out = []
    for e in ENGINES.values():
        if info["format"] in e.formats and e.available(hw)[0]:
            out.append((e, e.placements(hw, info)))
    return out


async def resolve(ref: str) -> tuple[Engine, dict]:
    """Motor y especificación (ruta, ubicación…) con que se ejecutará el modelo `ref`."""
    hw = hardware.detect()
    info = formats.detect(ref)
    s = load_settings()
    want = model_setting(ref, s)
    spec = {"ref": ref, **info}
    if info["format"] == "ollama":
        if not OLLAMA.online:
            await OLLAMA.probe()
        spec.update(placement=want["placement"] if want["placement"] in ("gpu", "cpu", "mixed") else "auto",
                    gpu_layers=want["gpu_layers"])
        return OLLAMA, spec
    if info["format"] == "unknown":
        raise EngineUnavailable(f"No reconozco el formato del modelo «{ref}»")
    cands = candidates(ref, hw, info)
    if want["engine"] != "auto":
        cands = [c for c in cands if c[0].id == want["engine"]]
        if not cands:
            e = ENGINES.get(want["engine"])
            why = e.available(hw)[1] if e else "motor desconocido"
            raise EngineUnavailable(f"El motor elegido para «{ref}» no está disponible: {why}")
    if not cands:
        able = [e.name for e in ENGINES.values() if info["format"] in e.formats]
        raise EngineUnavailable(f"Ningún motor disponible puede cargar «{ref}» ({formats.LABELS.get(info['format'])}). "
                                f"Instala uno de estos en ⚙️ Modelos → Motores: {', '.join(able)}")
    # Ubicación pedida
    if want["placement"] != "auto":
        ok = [(e, ps) for e, ps in cands if want["placement"] in ps]
        if ok:
            e = ok[0][0] if len(ok) == 1 else max(ok, key=lambda c: estimate(c[0].id, want["placement"], hw))[0]
            spec.update(engine=e.id, placement=want["placement"], gpu_layers=want["gpu_layers"])
            return e, spec
    # Por pruebas de velocidad
    bench = s["bench"].get(ref, {})
    best = None
    for e, ps in cands:
        for p in ps:
            r = bench.get(f"{e.id}|{p}")
            if r and r.get("tps") and (best is None or r["tps"] > best[2]):
                best = (e, p, r["tps"])
    if best and want["placement"] == "auto":
        spec.update(engine=best[0].id, placement=best[1], gpu_layers=want["gpu_layers"])
        return best[0], spec
    # Por estimación
    ranked = sorted(((e, _auto_placement(e, ps, hw, info)) for e, ps in cands),
                    key=lambda c: estimate(c[0].id, c[1], hw), reverse=True)
    e, p = ranked[0]
    spec.update(engine=e.id, placement=p, gpu_layers=want["gpu_layers"])
    return e, spec


# ------------------------------------------------------------------ estado para la interfaz

async def status() -> dict:
    hw = hardware.detect()
    await OLLAMA.probe()
    s = load_settings()
    models = []
    refs = {config.MODEL_CHAT, config.MODEL_CODE, config.MODEL_VISION} | set(s["models"]) | \
        {m["ref"] for m in formats.list_local()}
    for ref in sorted(r for r in refs if r):
        info = formats.detect(ref)
        try:
            e, spec = await resolve(ref)
            chosen = {"engine": e.id, "engine_name": e.name, "placement": spec.get("placement", "auto"), "error": ""}
        except Exception as ex:  # noqa: BLE001
            chosen = {"engine": "", "engine_name": "", "placement": "", "error": str(ex)}
        options = [{"engine": e.id, "name": e.name, "placements": ps} for e, ps in candidates(ref, hw, info)]
        if info["format"] == "ollama":
            options = [{"engine": "ollama", "name": OLLAMA.name, "placements": OLLAMA.placements(hw, info)}]
        models.append({"ref": ref, "format": info["format"], "format_label": formats.LABELS.get(info["format"], "?"),
                       "size_gb": info.get("size_gb", 0), "setting": model_setting(ref, s), "chosen": chosen,
                       "options": options, "bench": s["bench"].get(ref, {}),
                       "roles": [r for r in ROLES if getattr(config, f"MODEL_{r.upper()}") == ref]})
    return {"hardware": hw, "engines": [e.describe(hw) for e in ENGINES.values()], "models": models,
            "placements": PLACEMENTS, "models_dir": str(config.MODELS_DIR),
            "roles": {r: getattr(config, f"MODEL_{r.upper()}") for r in ROLES}}


def update(body: dict) -> dict:
    s = load_settings()
    for ref, m in (body.get("models") or {}).items():
        cur = s["models"].setdefault(ref, {})
        for k in ("engine", "placement", "gpu_layers"):
            if k in m:
                cur[k] = m[k]
        if cur.get("engine") in ("", "auto") and cur.get("placement") in ("", "auto") and not cur.get("gpu_layers"):
            s["models"].pop(ref, None)
    for role, ref in (body.get("roles") or {}).items():
        if role in ROLES:
            s["roles"][role] = ref
    save_settings(s)
    apply_roles(s)
    return s


async def unload_all() -> None:
    for e in ENGINES.values():
        try:
            await e.unload()
        except Exception:
            pass


# ------------------------------------------------------------------ prueba de velocidad

BENCH_PROMPT = ("Explica en unas 150 palabras cómo funciona la fotosíntesis, con un ejemplo. "
                "Responde en español y sin listas.")


async def benchmark(ref: str, max_tokens: int = 96) -> AsyncIterator[dict]:
    """Prueba cada combinación motor + ubicación posible para el modelo y guarda los tokens/segundo."""
    hw = hardware.detect()
    info = formats.detect(ref)
    if info["format"] == "ollama":
        await OLLAMA.probe()
        combos = [(OLLAMA, p) for p in OLLAMA.placements(hw, info)] if OLLAMA.online else []
    else:
        combos = [(e, p) for e, ps in candidates(ref, hw, info) for p in ps if p != "mixed"]
    if not combos:
        yield {"status": "error", "error": "No hay motores disponibles para este modelo"}
        return
    s = load_settings()
    results = s["bench"].setdefault(ref, {})
    for e, p in combos:
        label = f"{e.name} · {PLACEMENTS[p]}"
        yield {"status": "running", "combo": f"{e.id}|{p}", "label": label}
        spec = {"ref": ref, **info, "engine": e.id, "placement": p, "gpu_layers": 0}
        msgs = [{"role": "user", "content": BENCH_PROMPT}]
        t0 = time.perf_counter()
        first, n = None, 0
        try:
            # 1.ª pasada: carga y calentamiento (no se mide)
            async for kind, _ in e.chat_events(spec, [{"role": "user", "content": "Hola"}], None,
                                               {"num_predict": 8} if e.id == "ollama" else None):
                if kind == "text":
                    break
            t0 = time.perf_counter()
            agen = e.chat_events(spec, msgs, None, {"num_predict": max_tokens} if e.id == "ollama" else None)
            async for kind, _val in agen:
                if kind != "text":
                    continue
                n += 1
                if first is None:
                    first = time.perf_counter()
                if n >= max_tokens:
                    break
            await agen.aclose()
            dt = time.perf_counter() - (first or t0)
            r = {"tps": round((n - 1) / dt, 1) if n > 1 and dt > 0 else 0.0,
                 "ttft": round((first or time.perf_counter()) - t0, 2), "at": time.strftime("%Y-%m-%d %H:%M")}
            results[f"{e.id}|{p}"] = r
            yield {"status": "result", "combo": f"{e.id}|{p}", "label": label, **r}
        except Exception as ex:  # noqa: BLE001
            results.pop(f"{e.id}|{p}", None)
            yield {"status": "result", "combo": f"{e.id}|{p}", "label": label, "error": f"{type(ex).__name__}: {ex}"[:400]}
        finally:
            if e.id == "ollama":
                await OLLAMA.unload_model(ref)
            else:
                await e.unload()
    save_settings(s)
    best = max(((k, v) for k, v in results.items() if v.get("tps")), key=lambda kv: kv[1]["tps"], default=None)
    yield {"status": "done", "best": best[0] if best else None, "results": results}


# ------------------------------------------------------------------ instalación de motores

async def install(engine_id: str) -> AsyncIterator[str]:
    """Instala el paquete pip del motor en ENGINE_PACKS_DIR (no toca el resto del estudio)."""
    e = ENGINES.get(engine_id)
    pkgs = e.install if e else None
    if not pkgs:
        yield json.dumps({"error": "Este motor no se instala desde el estudio"}) + "\n"
        return
    PACKS_SITE.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check",
           "--target", str(PACKS_SITE), *pkgs]
    yield json.dumps({"status": f"Instalando {e.name}: {' '.join(p for p in pkgs if not p.startswith('-'))}"}) + "\n"
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    assert proc.stdout
    async for line in proc.stdout:
        text = line.decode(errors="replace").strip()
        if text:
            yield json.dumps({"status": text[:300]}) + "\n"
    code = await proc.wait()
    importlib.invalidate_caches()
    hardware.refresh()
    if code == 0:
        yield json.dumps({"status": "success", "done": True}) + "\n"
    else:
        yield json.dumps({"error": f"pip terminó con código {code}"}) + "\n"


# ------------------------------------------------------------------ descarga de modelos (Hugging Face)

async def download(repo: str, allow: list[str] | None = None) -> AsyncIterator[str]:
    """Descarga un repositorio de Hugging Face a MODELS_DIR/<org>--<nombre>."""
    from huggingface_hub import snapshot_download

    name = repo.strip().strip("/").replace("/", "--")
    target = config.MODELS_DIR / name
    config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    yield json.dumps({"status": f"Descargando {repo} en {target}"}) + "\n"
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(None, lambda: snapshot_download(repo_id=repo.strip(), local_dir=str(target),
                                                               allow_patterns=allow or None))
    while not fut.done():
        await asyncio.sleep(2)
        size = formats._size(target) if target.exists() else 0
        yield json.dumps({"status": f"{size / 1024 ** 3:.2f} GB descargados"}) + "\n"
    try:
        fut.result()
    except Exception as ex:  # noqa: BLE001
        yield json.dumps({"error": f"{type(ex).__name__}: {ex}"[:500]}) + "\n"
        return
    info = formats.detect(name)
    yield json.dumps({"status": "success", "done": True, "ref": name, "format": info["format"]}) + "\n"


def capabilities_sync(ref: str) -> set[str]:
    info = formats.detect(ref)
    return {"completion", "tools"} | ({"vision"} if info.get("mmproj") else set())


__all__ = ["ENGINES", "OLLAMA", "resolve", "status", "update", "benchmark", "install", "download",
           "apply_roles", "unload_all", "EngineUnavailable"]
