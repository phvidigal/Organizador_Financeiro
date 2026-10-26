"""Totais do período: receita, despesa e para onde foi o dinheiro."""

import uuid
from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import get_tenant_session
from app.schemas.dashboard import DashboardSummaryRead
from app.services.dashboard import load_summary

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=DashboardSummaryRead)
async def get_summary(
    # Obrigatórios: um total sem período é um número que muda de significado a cada
    # sync, e a tela sempre sabe qual mês está mostrando.
    date_from: date_type = Query(description="primeiro dia do período, inclusive"),
    date_to: date_type = Query(description="último dia do período, inclusive"),
    account_id: uuid.UUID | None = Query(None, description="restringe a uma conta"),
    session: AsyncSession = Depends(get_tenant_session),
) -> DashboardSummaryRead:
    """Receita e despesa somadas por `kind`, com a quebra por categoria.

    `TRANSFER` vem separado e fora do `net`. Cada total traz a parcela ainda não
    decidida (`PENDING`, `NEEDS_REVIEW`, `FAILED`) — que está dentro dele: tirá-la
    esconderia gasto real, e é a tela que avisa o quanto do número é provisório.
    """
    if date_from > date_to:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "date_from posterior a date_to"
        )

    summary = await load_summary(
        session, date_from=date_from, date_to=date_to, account_id=account_id
    )
    return DashboardSummaryRead.model_validate(summary)
