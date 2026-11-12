"""Reconoce el formato de un modelo a partir de su ruta (o de su nombre en Ollama).

Cada motor lee formatos distintos; el formato decide qué motores pueden cargar el modelo:
  ollama      nombre de la librería de Ollama (qwen3:8b)          → Ollama (llama.cpp)
  gguf        archivo .gguf                                       → llama-server, OpenVINO GenAI
  onnx        carpeta con genai_config.json                       → ONNX Runtime GenAI
  openvino    carpeta con openvino_model.xml                      → OpenVINO GenAI
  mlc         carpeta con mlc-chat-config.json                    → MLC LLM
  tensorrt    carpeta de motores TensorRT-LLM o checkpoint HF     → TensorRT-LLM
  executorch  archivo .pte (+ tokenizer)                          → ExecuTorch
  litert      archivo .litertlm / .task                           → LiteRT-LM
  genie       carpeta con genie_config.json (Qualcomm AI Hub)     → Qualcomm Genie
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import config

LABELS = {
    "ollama": "Ollama (GGUF)", "gguf": "GGUF", "onnx": "ONNX GenAI", "openvino": "OpenVINO IR",
    "mlc": "MLC", "tensorrt": "TensorRT-LLM", "hf": "Hugging Face (safetensors)", "executorch": "ExecuTorch .pte",
    "litert": "LiteRT-LM", "genie": "Qualcomm Genie",
}


def resolve_path(ref: str) -> Path | None:
    """Ruta local del modelo: absoluta o relativa a MODELS_DIR. None si es un nombre de Ollama."""
    if not ref:
        return None
    p = Path(ref).expanduser()
    if p.is_absolute():
        return p if p.exists() else None
    q = config.MODELS_DIR / ref
    return q if q.exists() else None


def _first(d: Path, *patterns: str) -> Path | None:
    for pat in patterns:
        hit = next(iter(sorted(d.glob(pat))), None)
        if hit:
            return hit
    return None


def detect(ref: str) -> dict:
    """{"format", "path", "size_gb", ...}. Para nombres que no son rutas devuelve formato "ollama"."""
    p = resolve_path(ref)
    if p is None:
        return {"format": "ollama", "path": None, "size_gb": 0.0}
    info: dict = {"path": str(p), "size_gb": round(_size(p) / 1024 ** 3, 2)}
    if p.is_file():
        ext = p.suffix.lower()
        fmt = {".gguf": "gguf", ".pte": "executorch", ".litertlm": "litert", ".task": "litert"}.get(ext, "")
        if fmt == "executorch":
            tok = _first(p.parent, "tokenizer.json", "tokenizer.model", "*.tiktoken", "tokenizer.bin")
            info["tokenizer"] = str(tok) if tok else ""
        return {**info, "format": fmt or "unknown"}
    if (p / "genai_config.json").exists():
        fmt = "onnx"
        try:  # modelos ONNX para NPU Qualcomm (QNN) o con proveedor fijo
            prov = json.loads((p / "genai_config.json").read_text())["model"]["decoder"]["session_options"].get(
                "provider_options", [])
            info["providers"] = [next(iter(x)) for x in prov if isinstance(x, dict) and x]
        except (ValueError, KeyError, TypeError, StopIteration):
            pass
    elif (p / "openvino_model.xml").exists() or (p / "openvino_language_model.xml").exists():
        fmt = "openvino"
    elif (p / "mlc-chat-config.json").exists():
        fmt = "mlc"
    elif (p / "genie_config.json").exists() or _first(p, "*genie*config*.json"):
        fmt = "genie"
        info["config"] = str((p / "genie_config.json") if (p / "genie_config.json").exists()
                             else _first(p, "*genie*config*.json"))
    elif _first(p, "rank0.engine", "*.engine"):
        fmt = "tensorrt"
    elif _first(p, "*.pte"):
        pte = _first(p, "*.pte")
        info["path"] = str(pte)
        tok = _first(p, "tokenizer.json", "tokenizer.model", "*.tiktoken", "tokenizer.bin")
        info["tokenizer"] = str(tok) if tok else ""
        fmt = "executorch"
    elif _first(p, "*.litertlm", "*.task"):
        info["path"] = str(_first(p, "*.litertlm", "*.task"))
        fmt = "litert"
    elif _first(p, "*.gguf"):
        ggufs = [g for g in sorted(p.glob("*.gguf")) if "mmproj" not in g.name.lower()]
        info["path"] = str(ggufs[0] if ggufs else _first(p, "*.gguf"))
        mm = _first(p, "mmproj*.gguf", "*mmproj*.gguf")
        if mm:
            info["mmproj"] = str(mm)
        fmt = "gguf"
    elif (p / "config.json").exists() and _first(p, "*.safetensors"):
        fmt = "hf"  # checkpoint de Hugging Face: TensorRT-LLM (backend PyTorch) lo carga directamente
    else:
        fmt = "unknown"
    info["format"] = fmt
    return info


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total


def list_local() -> list[dict]:
    """Modelos guardados en MODELS_DIR (una carpeta o archivo por modelo)."""
    out = []
    if not config.MODELS_DIR.exists():
        return out
    for p in sorted(config.MODELS_DIR.iterdir()):
        if p.name.startswith(".") or p.name.endswith(".partial"):
            continue
        d = detect(p.name)
        if d["format"] != "unknown":
            out.append({"ref": p.name, **d})
    return out
