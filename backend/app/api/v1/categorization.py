"""Disparo e acompanhamento da categorização por LLM."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_ollama_client
from app.core.tenancy import get_tenant_session, resolve_tenant_id
from app.models.enums import CategorizationStatus, CategorySource
from app.models.transaction import Transaction
from app.schemas.categorization import (
    CategorizationOutcomeRead,
    CategorizationResetRead,
    CategorizationStatusRead,
    QueueCountsRead,
)
from app.services.categorization import runner
from app.services.categorization.client import OllamaClient
from app.services.categorization.store import reset_categorization

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/categorization", tags=["categorization"])


async def _queue_counts(session: AsyncSession) -> QueueCountsRead:
    """Fila por status. Uma consulta só, agrupada."""
    result = await session.execute(
        select(Transaction.categorization_status, func.count())
        .where(Transaction.deleted_at.is_(None))
        .group_by(Transaction.categorization_status)
    )
    counts = {row[0]: row[1] for row in result}
    return QueueCountsRead(
        pending=counts.get(CategorizationStatus.PENDING.value, 0),
        categorized=counts.get(CategorizationStatus.CATEGORIZED.value, 0),
        needs_review=counts.get(CategorizationStatus.NEEDS_REVIEW.value, 0),
        failed=counts.get(CategorizationStatus.FAILED.value, 0),
    )


async def _status(session: AsyncSession, tenant_id: uuid.UUID) -> CategorizationStatusRead:
    outcome = runner.last_outcome(tenant_id)
    return CategorizationStatusRead(
        tenant_id=tenant_id,
        running=runner.is_running(tenant_id),
        queue=await _queue_counts(session),
        last_outcome=(
            CategorizationOutcomeRead(
                status=outcome.status,
                started_at=outcome.started_at,
                finished_at=outcome.finished_at,
                model=outcome.model,
                processed=outcome.processed,
                categorized=outcome.categorized,
                needs_review=outcome.needs_review,
                failed=outcome.failed,
                error=outcome.error,
                warnings=outcome.warnings,
            )
            if outcome is not None
            else None
        ),
    )


@router.post("/run", status_code=status.HTTP_202_ACCEPTED)
async def start_categorization(
    response: Response,
    limit: int | None = Query(
        None,
        ge=1,
        description="máximo de transações nesta execução; útil para conferir o prompt "
        "em dez linhas antes de gastar o backlog inteiro",
    ),
    tenant_id: uuid.UUID = Depends(resolve_tenant_id),
    session: AsyncSession = Depends(get_tenant_session),
    client: OllamaClient = Depends(get_ollama_client),
) -> CategorizationStatusRead:
    """Dispara a categorização e devolve **202** imediatamente.

    Nunca bloqueia: são segundos por transação, e o Ollama serializa as chamadas —
    o backlog inicial de algumas centenas de linhas leva de dez a vinte minutos. A
    resposta já traz a fila para a tela pintar o progresso sem uma segunda chamada.

    Não há throttle por tempo, ao contrário do sync da Pluggy: aqui não se consome
    cota de terceiro, e um segundo disparo é recusado pelo lock com **409**.
    """
    started = runner.schedule_categorization(tenant_id=tenant_id, client=client, limit=limit)
    if not started:
        response.status_code = status.HTTP_409_CONFLICT
        logger.info("categorização do tenant %s já estava em andamento", tenant_id)

    return await _status(session, tenant_id)


@router.get("/status", response_model=CategorizationStatusRead)
async def get_categorization_status(
    tenant_id: uuid.UUID = Depends(resolve_tenant_id),
    session: AsyncSession = Depends(get_tenant_session),
) -> CategorizationStatusRead:
    """Estado da categorização. É este o endpoint que a interface põe em polling."""
    return await _status(session, tenant_id)


@router.post("/reset", response_model=CategorizationResetRead)
async def reset(
    source: CategorySource = Query(
        CategorySource.LLM,
        description="origem cujas decisões voltam para a fila",
    ),
    session: AsyncSession = Depends(get_tenant_session),
) -> CategorizationResetRead:
    """Devolve à fila as transações decididas por uma origem.

    Existe para a iteração do prompt: mudou o texto ou o limiar, reseta e roda de
    novo. Sem isto, a única forma de reprocessar seria um `UPDATE` na mão — e quem o
    escreve esquece de reverter o `kind`, deixando um Pix marcado como TRANSFER
    apontando para categoria nenhuma.

    `MANUAL` é recusado: a correção do usuário é a régua para medir o acerto do LLM
    e a base da futura pipeline de regras. Nenhum caminho do sistema pode apagá-la.
    """
    try:
        affected = await reset_categorization(session, source=source)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None

    return CategorizationResetRead(source=source.value, transactions_reset=affected)
