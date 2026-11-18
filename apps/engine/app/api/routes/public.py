"""Superfícies públicas: só as imagens geradas pelo motor.

Landing pages, formulários de leads, redirecionador de afiliados e opt-out do
upstream saíram na E2.2 (fora do escopo da v1, D-016).
"""
from __future__ import annotations

import pathlib

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.core.config import settings

router = APIRouter(tags=["public"])


def _safe_file(directory: str, filename: str) -> pathlib.Path:
    """Impede path traversal: o arquivo tem de estar dentro do diretório."""
    base = pathlib.Path(directory).resolve()
    path = (base / filename).resolve()
    if not str(path).startswith(str(base) + "/") or not path.is_file():
        raise HTTPException(404, "Arquivo não encontrado")
    return path


@router.get("/media/{filename}", summary="Imagem gerada pelo motor")
def media(filename: str):
    try:
        path = _safe_file(settings.media_dir, filename)
    except HTTPException:  # capas por IA ficam em `illustrations/` (illustration.save)
        path = _safe_file(str(pathlib.Path(settings.media_dir) / "illustrations"), filename)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=604800"})
