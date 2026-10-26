"""Roteador raiz da v1.

Sem prefixo global de propósito: `/health` é caminho absoluto e o painel do
frontend depende disso. Cada router declara o seu.
"""

from fastapi import APIRouter

from app.api.v1 import (
    accounts,
    categories,
    categorization,
    connections,
    dashboard,
    health,
    pluggy_diagnostics,
    transactions,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(connections.router)
api_router.include_router(accounts.router)
api_router.include_router(transactions.router)
api_router.include_router(categories.router)
api_router.include_router(categorization.router)
api_router.include_router(dashboard.router)
api_router.include_router(pluggy_diagnostics.router)
