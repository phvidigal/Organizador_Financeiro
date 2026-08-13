"""Dependências compartilhadas dos endpoints."""

from functools import lru_cache

from fastapi import HTTPException, status

from app.core.config import get_settings
from app.services.categorization.client import OllamaClient
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


@lru_cache
def _ollama_singleton() -> OllamaClient:
    """Um cliente para o processo inteiro, pelo pool de conexões do httpx.

    Sem o `503` que o cliente da Pluggy tem: o Ollama não tem credencial para estar
    faltando. Se ele estiver fora do ar, quem descobre é a execução do job — que
    aborta deixando as transações na fila — e o diagnóstico é `GET /health/ollama`.
    """
    settings = get_settings()
    return OllamaClient(base_url=settings.ollama_base_url, model=settings.ollama_model)


def get_ollama_client() -> OllamaClient:
    """Cliente do Ollama. Também usado fora de request, no gancho de pós-sync."""
    return _ollama_singleton()


async def close_ollama_client() -> None:
    """Fecha o pool do cliente no shutdown. Chamado pelo lifespan."""
    if _ollama_singleton.cache_info().currsize:
        await _ollama_singleton().aclose()
        _ollama_singleton.cache_clear()
