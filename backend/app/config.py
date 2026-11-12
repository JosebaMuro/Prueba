"""Configuración central. Todo se lee de variables de entorno (ver studio.env.example)."""
import os
from pathlib import Path


def _env(name: str, default: str) -> str:
    v = os.environ.get(name, "").strip()
    return v if v else default


def _bool(name: str, default: bool) -> bool:
    return _env(name, str(default)).lower() in ("1", "true", "yes", "si", "sí", "on")


OLLAMA_URL = _env("OLLAMA_URL", "http://localhost:11434")

# Modelos (todos servidos por Ollama, 100% locales).
# Por defecto el chat y el programador comparten modelo: así Ollama no tiene que descargar y
# recargar otro modelo de varios GB en la GPU cada vez que el supervisor delega en el programador.
MODEL_CHAT = _env("MODEL_CHAT", "qwen3:8b")                  # razonamiento + tool calling
MODEL_CODE = _env("MODEL_CODE", MODEL_CHAT)                  # agente programador
MODEL_VISION = _env("MODEL_VISION", "qwen3-vl:8b-instruct")  # imágenes / capturas / PDFs escaneados
MODEL_EMBED = _env("MODEL_EMBED", "qwen3-embedding:0.6b")    # embeddings multilingües para la memoria

# Rendimiento del motor (Ollama). Se envían en cada petición, así funcionan aunque Ollama
# ya estuviera abierto (p. ej. la app de la bandeja de Windows) con otra configuración.
# IMPORTANTE: NUM_CTX debe ser igual en todas las llamadas a un mismo modelo; si cambia, Ollama lo recarga.
NUM_CTX = int(_env("NUM_CTX", "8192"))          # contexto: 4096 trunca historial + herramientas
KEEP_ALIVE = _env("KEEP_ALIVE", "30m")          # tiempo que el modelo sigue cargado tras usarse
THINK = _bool("THINK", False)                   # razonamiento "thinking" de Qwen3: más lento, a veces mejor
TEMPERATURE = float(_env("TEMPERATURE", "0.6"))

# Memoria vectorial: si QDRANT_URL está vacío se usa Qdrant embebido en disco
QDRANT_URL = _env("QDRANT_URL", "")
DATA_DIR = Path(_env("DATA_DIR", "./data")).resolve()
WORKSPACE_DIR = Path(_env("WORKSPACE_DIR", "./workspace")).resolve()

# Servicios multimodales opcionales
WHISPER_MODEL = _env("WHISPER_MODEL", "small")         # faster-whisper: tiny/base/small/medium/large-v3-turbo
WHISPER_LANGUAGE = _env("WHISPER_LANGUAGE", "")        # p. ej. "es"; vacío = detección automática
WHISPER_BEAM = int(_env("WHISPER_BEAM", "1"))          # 1 = voraz (2-3x más rápido), 5 = algo más preciso
PIPER_VOICE = _env("PIPER_VOICE", "")                  # ruta a un .onnx de Piper, p. ej. es_ES-davefx-medium.onnx
COMFYUI_URL = _env("COMFYUI_URL", "")                  # p. ej. http://comfyui:8188

# Agentes y seguridad de la herramienta de ejecución de código
ENABLE_CODE_EXEC = _bool("ENABLE_CODE_EXEC", True)
CODE_EXEC_TIMEOUT = int(_env("CODE_EXEC_TIMEOUT", "30"))
MAX_AGENT_STEPS = int(_env("MAX_AGENT_STEPS", "8"))
TOOL_RESULT_CHARS = int(_env("TOOL_RESULT_CHARS", "8000"))  # lo que ve el modelo de cada resultado
WARMUP = _bool("WARMUP", True)                         # precargar el modelo de chat al arrancar

DATA_DIR.mkdir(parents=True, exist_ok=True)
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

# Acceso por red local (app Android). Si se define, todas las llamadas a /api lo exigen.
ACCESS_TOKEN = _env("ACCESS_TOKEN", "")
HOST = _env("HOST", "127.0.0.1")
PORT = int(_env("PORT", "8765"))
FRONTEND_DIR = Path(_env("FRONTEND_DIR", str(Path(__file__).resolve().parents[2] / "frontend"))).resolve()
