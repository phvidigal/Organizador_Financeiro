"""Health checks."""

from typing import Any

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_session

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    """O processo está de pé. Sem dependências — é o healthcheck do container.

    Separado do /health porque um healthcheck que falha quando o banco cai faria
    o Docker reiniciar o backend em loop durante uma indisponibilidade do Postgres,
    sem resolver nada.
    """
    return {"status": "ok"}


@router.get("/health")
async def health(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Prontidão: o backend consegue falar com o banco?"""
    try:
        await session.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception as exc:  # pragma: no cover - caminho de indisponibilidade
        db_status = f"error: {type(exc).__name__}"

    return {"status": "ok" if db_status == "ok" else "degraded", "db": db_status}


@router.get("/health/ollama")
async def ollama_health(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """O container alcança o Ollama do host e o modelo configurado está lá?

    Existe já na Fase 0 de propósito: o Ollama roda fora do Docker, então a rota
    container → host é a peça mais provável de falhar em outra máquina. Descobrir
    isso agora custa um endpoint; descobrir na Fase 3 custa depurar a pipeline de
    categorização inteira antes de achar que o problema era de rede.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{settings.ollama_base_url}/api/tags")
            response.raise_for_status()
            models = [m["name"] for m in response.json().get("models", [])]
    except Exception as exc:
        return {
            "status": "unreachable",
            "base_url": settings.ollama_base_url,
            "error": f"{type(exc).__name__}: {exc}",
        }

    return {
        "status": "ok",
        "base_url": settings.ollama_base_url,
        "expected_model": settings.ollama_model,
        "model_available": settings.ollama_model in models,
        "models": models,
    }
