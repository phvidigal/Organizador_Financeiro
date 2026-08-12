"""Categorias e o de/para com a taxonomia da Pluggy."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_pluggy_client
from app.core.tenancy import get_tenant_session
from app.schemas.connection import CategoryMapReportRead
from app.services.pluggy.category_map import sync_category_map
from app.services.pluggy.client import PluggyClient

router = APIRouter(prefix="/categories", tags=["categories"])


@router.post("/pluggy-map", response_model=CategoryMapReportRead)
async def refresh_pluggy_map(
    session: AsyncSession = Depends(get_tenant_session),
    client: PluggyClient = Depends(get_pluggy_client),
) -> CategoryMapReportRead:
    """Reexecuta o de/para de categorias contra `GET /categories`.

    O sync já faz isso, mas avulso é o que permite editar `PLUGGY_TO_LOCAL` e ver
    o efeito sem esperar a próxima sincronização — que, no tier pessoal, pode
    levar 24 horas para trazer algo novo.

    Idempotente. O relatório é o insumo para completar o dicionário:
    `unmatched_pluggy` lista o que eles têm e nós ignoramos, `unmapped_local`
    lista as nossas que continuam sem contraparte.
    """
    report = await sync_category_map(session, pluggy_categories=await client.list_categories())
    return CategoryMapReportRead(**vars(report))
