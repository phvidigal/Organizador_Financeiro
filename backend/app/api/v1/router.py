"""Roteador raiz da v1.

Endpoints de domínio (contas, transações, categorias) entram nas Fases 2 a 4.
"""

from fastapi import APIRouter

from app.api.v1 import health

api_router = APIRouter()
api_router.include_router(health.router)
