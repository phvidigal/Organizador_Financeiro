"""Ponto de entrada da API."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.db import engine
from app.core.rls_guard import assert_rls_enforced


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Verificação de isolamento antes de aceitar o primeiro request. Se o role da
    # conexão puder ignorar as policies, o processo morre aqui — ver o cabeçalho
    # de app/core/rls_guard.py.
    await assert_rls_enforced(engine)

    yield

    # Fecha o pool no shutdown; sem isso o --reload deixa conexões penduradas no
    # Postgres a cada reinício até estourar o max_connections.
    await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(
        title="Organizador Financeiro",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)
    return app


app = create_app()
