"""Dependências compartilhadas dos endpoints."""

from functools import lru_cache

from fastapi import HTTPException, status

from app.core.config import get_settings
from app.services.pluggy.client import PluggyClient


@lru_cache
def _pluggy_singleton() -> PluggyClient:
    """Um cliente para o processo inteiro.

    A API key vive ~2 horas e fica na instância; criar um cliente por request
    jogaria o cache fora e faria um `POST /auth` a cada chamada, dobrando a
    latência do sync. O pool de conexões do httpx também se beneficia.
    """
    settings = get_settings()
    return PluggyClient(
        client_id=settings.pluggy_client_id or "",
        client_secret=settings.pluggy_client_secret or "",
        base_url=settings.pluggy_base_url,
    )


def get_pluggy_client() -> PluggyClient:
    """Cliente da Pluggy, ou 503 se as credenciais não estiverem configuradas.

    503 no endpoint e não erro no import: sem isso, um `docker compose up` com o
    `.env` ainda vazio derrubaria a API inteira — inclusive o `/health` que o
    frontend usa para mostrar o que está faltando.
    """
    settings = get_settings()
    if not settings.pluggy_client_id or not settings.pluggy_client_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Credenciais da Pluggy não configuradas "
                "(PLUGGY_CLIENT_ID / PLUGGY_CLIENT_SECRET)"
            ),
        )
    return _pluggy_singleton()


async def close_pluggy_client() -> None:
    """Fecha o pool do cliente no shutdown. Chamado pelo lifespan."""
    if _pluggy_singleton.cache_info().currsize:
        await _pluggy_singleton().aclose()
        _pluggy_singleton.cache_clear()
