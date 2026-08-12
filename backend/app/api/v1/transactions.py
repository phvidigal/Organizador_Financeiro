"""Listagem de transações."""

import uuid
from datetime import date as date_type

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import get_tenant_session
from app.models.transaction import Transaction
from app.schemas.common import Page
from app.schemas.transaction import TransactionRead

router = APIRouter(prefix="/transactions", tags=["transactions"])

MAX_LIMIT = 200


@router.get("", response_model=Page[TransactionRead])
async def list_transactions(
    account_id: uuid.UUID | None = Query(None),
    date_from: date_type | None = Query(None),
    date_to: date_type | None = Query(None),
    kind: str | None = Query(None, description="INCOME, EXPENSE ou TRANSFER"),
    categorization_status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_tenant_session),
) -> Page[TransactionRead]:
    """Extrato paginado, do mais recente para o mais antigo.

    O `total` sai de um `count()` separado porque a interface precisa saber quando
    parar de paginar, e a tabela é pequena o bastante para isso não pesar.
    """
    filters = [Transaction.deleted_at.is_(None)]
    if account_id is not None:
        filters.append(Transaction.account_id == account_id)
    if date_from is not None:
        filters.append(Transaction.date >= date_from)
    if date_to is not None:
        filters.append(Transaction.date <= date_to)
    if kind is not None:
        filters.append(Transaction.kind == kind)
    if categorization_status is not None:
        filters.append(Transaction.categorization_status == categorization_status)

    total = await session.scalar(select(func.count()).select_from(Transaction).where(*filters)) or 0

    result = await session.scalars(
        select(Transaction)
        .where(*filters)
        # `created_at` desempata: várias transações no mesmo dia sem critério
        # secundário sairiam em ordem indefinida, e a paginação repetiria ou
        # pularia linhas entre uma página e outra.
        .order_by(Transaction.date.desc(), Transaction.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    return Page[TransactionRead](
        items=[TransactionRead.model_validate(row) for row in result],
        total=total,
        limit=limit,
        offset=offset,
    )
