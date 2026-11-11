"""Workspace: carpeta compartida entre el usuario y los agentes (montada en ./workspace).

Todas las rutas se resuelven dentro de WORKSPACE_DIR; cualquier intento de salir
(../, rutas absolutas, symlinks) se rechaza.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import config

TEXT_EXT = {".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".html", ".css", ".yaml", ".yml",
            ".toml", ".sh", ".sql", ".xml", ".ini", ".log", ".tsx", ".jsx", ".java", ".go", ".rs", ".c",
            ".cpp", ".h", ".rb", ".php"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


class WorkspaceError(ValueError):
    pass


def resolve(rel: str) -> Path:
    root = config.WORKSPACE_DIR
    p = (root / rel.lstrip("/")).resolve()
    if p != root and root not in p.parents:
        raise WorkspaceError(f"Ruta fuera del workspace: {rel}")
    return p


def rel(p: Path) -> str:
    return str(p.relative_to(config.WORKSPACE_DIR))


def tree(sub: str = "") -> list[dict]:
    base = resolve(sub)
    if not base.exists():
        return []
    out = []
    for p in sorted(base.rglob("*")):
        if any(part.startswith(".") for part in p.relative_to(config.WORKSPACE_DIR).parts):
            continue
        out.append({"path": rel(p), "dir": p.is_dir(), "size": p.stat().st_size if p.is_file() else 0})
    return out


def read_text(path: str, max_chars: int = 60_000) -> str:
    p = resolve(path)
    if not p.is_file():
        raise WorkspaceError(f"No existe el archivo: {path}")
    if p.suffix.lower() == ".pdf":
        return pdf_text(p)[:max_chars]
    if p.suffix.lower() == ".docx":
        return docx_text(p)[:max_chars]
    return p.read_text(encoding="utf-8", errors="replace")[:max_chars]


def write_text(path: str, content: str) -> str:
    p = resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return rel(p)


def write_bytes(path: str, data: bytes) -> str:
    p = resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return rel(p)


def delete(path: str) -> None:
    p = resolve(path)
    if p == config.WORKSPACE_DIR:
        raise WorkspaceError("No se puede borrar la raíz del workspace")
    if p.is_dir():
        shutil.rmtree(p)
    elif p.exists():
        p.unlink()


def is_indexable(path: str) -> bool:
    return Path(path).suffix.lower() in TEXT_EXT | {".pdf", ".docx"}


def pdf_text(p: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "(pypdf no instalado)"
    return "\n".join((page.extract_text() or "") for page in PdfReader(str(p)).pages)


def docx_text(p: Path) -> str:
    try:
        import docx  # python-docx
    except ImportError:
        return "(python-docx no instalado)"
    return "\n".join(par.text for par in docx.Document(str(p)).paragraphs)
