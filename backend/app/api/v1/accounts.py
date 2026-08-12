"""Contas sincronizadas."""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import get_tenant_session
from app.models.account import Account
from app.schemas.account import AccountRead

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("", response_model=list[AccountRead])
async def list_accounts(
    connection_id: uuid.UUID | None = Query(None, description="filtra por conexão bancária"),
    session: AsyncSession = Depends(get_tenant_session),
) -> list[Account]:
    stmt = select(Account).where(Account.deleted_at.is_(None))
    if connection_id is not None:
        stmt = stmt.where(Account.bank_connection_id == connection_id)

    # Cartão antes de conta corrente seria arbitrário; o nome é estável e a lista
    # é curta o bastante para a ordenação ser só previsibilidade.
    result = await session.scalars(stmt.order_by(Account.type, Account.name))
    return list(result)
