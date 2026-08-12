"""Conexões bancárias e disparo de sincronização."""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_pluggy_client
from app.core.tenancy import get_tenant_session, resolve_tenant_id
from app.models.bank_connection import BankConnection
from app.schemas.connection import (
    AdoptItemRequest,
    BankConnectionRead,
    CategoryMapReportRead,
    SyncOutcomeRead,
    SyncStatusRead,
)
from app.services.pluggy import runner
from app.services.pluggy.client import PluggyClient
from app.services.pluggy.errors import PluggyNotFoundError
from app.services.pluggy.mappers import map_item

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/connections", tags=["connections"])

# Intervalo mínimo entre dois syncs da mesma conexão.
#
# Dez minutos é folgado porque, no tier pessoal, a Pluggy só coleta dado novo a
# cada ~24h (ver `next_auto_sync_at`): sincronizar mais que isso relê exatamente o
# mesmo conteúdo e gasta cota. O `force=true` existe para desenvolvimento.
MIN_SYNC_INTERVAL = timedelta(minutes=10)


async def _get_connection(session: AsyncSession, connection_id: uuid.UUID) -> BankConnection:
    """Busca a conexão do tenant corrente, ou 404.

    Sob RLS, "não existe" e "é de outro tenant" são indistinguíveis daqui — e é
    exatamente o modo de falha desejado: a resposta não revela a existência de
    dado alheio.
    """
    connection = await session.scalar(
        select(BankConnection).where(
            BankConnection.id == connection_id, BankConnection.deleted_at.is_(None)
        )
    )
    if connection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conexão não encontrada")
    return connection


def _sync_status(connection: BankConnection, **overrides) -> SyncStatusRead:
    outcome = runner.last_outcome(connection.id)
    return SyncStatusRead(
        connection_id=connection.id,
        running=runner.is_running(connection.id),
        status=connection.status,
        execution_status=connection.execution_status,
        last_synced_at=connection.last_synced_at,
        last_success_at=connection.last_success_at,
        next_auto_sync_at=connection.next_auto_sync_at,
        consent_expires_at=connection.consent_expires_at,
        error=connection.error,
        last_outcome=(
            SyncOutcomeRead(
                status=outcome.status,
                started_at=outcome.started_at,
                finished_at=outcome.finished_at,
                accounts_synced=outcome.accounts_synced,
                transactions_upserted=outcome.transactions_upserted,
                item_refresh_supported=outcome.item_refresh_supported,
                warnings=outcome.warnings,
                error=outcome.error,
                categories=(
                    CategoryMapReportRead(**vars(outcome.categories))
                    if outcome.categories
                    else None
                ),
            )
            if outcome is not None
            else None
        ),
        **overrides,
    )


@router.post("", response_model=BankConnectionRead)
async def adopt_item(
    payload: AdoptItemRequest,
    response: Response,
    tenant_id: uuid.UUID = Depends(resolve_tenant_id),
    session: AsyncSession = Depends(get_tenant_session),
    client: PluggyClient = Depends(get_pluggy_client),
) -> BankConnection:
    """Adota um item existente na Pluggy e agenda a primeira sincronização.

    Consulta a Pluggy **antes** de gravar: confirma que o item existe e pertence a
    estas credenciais, e já traz connector e status para a linha nascer preenchida
    em vez de com placeholders que a primeira sync corrigiria.

    Reenviar o mesmo `itemId` é idempotente (200, não 409). Repetir uma ação que
    já produziu o efeito desejado não é conflito — e a constraint
    `uq_bank_connections_tenant_item` existe justamente para que não vire uma
    segunda conexão sincronizando as mesmas contas.
    """
    try:
        item = await client.get_item(payload.item_id)
    except PluggyNotFoundError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"A Pluggy não conhece o item {payload.item_id} para estas credenciais",
        ) from None

    columns = map_item(item)

    existing = await session.scalar(
        select(BankConnection).where(BankConnection.pluggy_item_id == payload.item_id)
    )
    if existing is not None:
        for field, value in columns.items():
            setattr(existing, field, value)
        # Readotar uma conexão excluída a traz de volta, em vez de esbarrar na
        # constraint com um erro que o usuário não teria como interpretar.
        existing.deleted_at = None
        connection = existing
        response.status_code = status.HTTP_200_OK
    else:
        connection = BankConnection(
            tenant_id=tenant_id,
            pluggy_item_id=payload.item_id,
            # Momento em que este app passou a usar a conexão. O consentimento em
            # si foi dado no meu.pluggy.ai; aqui fica o registro do nosso uso,
            # que é o que a Fase 5 precisa auditar.
            consent_granted_at=datetime.now(UTC),
            **columns,
        )
        session.add(connection)
        response.status_code = status.HTTP_201_CREATED

    await session.flush()

    runner.schedule_sync(tenant_id=tenant_id, connection_id=connection.id, client=client)
    return connection


@router.get("", response_model=list[BankConnectionRead])
async def list_connections(
    session: AsyncSession = Depends(get_tenant_session),
) -> list[BankConnection]:
    result = await session.scalars(
        select(BankConnection)
        .where(BankConnection.deleted_at.is_(None))
        .order_by(BankConnection.created_at)
    )
    return list(result)


@router.get("/{connection_id}", response_model=BankConnectionRead)
async def get_connection(
    connection_id: uuid.UUID,
    session: AsyncSession = Depends(get_tenant_session),
) -> BankConnection:
    return await _get_connection(session, connection_id)


@router.post("/{connection_id}/sync", status_code=status.HTTP_202_ACCEPTED)
async def start_sync(
    connection_id: uuid.UUID,
    response: Response,
    force: bool = Query(False, description="ignora o intervalo mínimo entre syncs"),
    full: bool = Query(False, description="relê todo o histórico, ignorando o watermark"),
    tenant_id: uuid.UUID = Depends(resolve_tenant_id),
    session: AsyncSession = Depends(get_tenant_session),
    client: PluggyClient = Depends(get_pluggy_client),
) -> SyncStatusRead:
    """Dispara uma sincronização e devolve **202** imediatamente.

    Nunca bloqueia: ler todas as contas e páginas de transações leva de segundos a
    minutos, e a resposta já traz o estado atual para a tela pintar
    "sincronizando…" sem precisar de uma segunda chamada.
    """
    connection = await session.scalar(
        select(BankConnection)
        .where(BankConnection.id == connection_id, BankConnection.deleted_at.is_(None))
        # `FOR UPDATE`: dois F5 simultâneos leriam o mesmo `last_synced_at` antigo e
        # ambos disparariam. O lock de linha serializa a decisão, e gravar
        # `last_synced_at` na mesma transação é o que **reserva** o sync.
        .with_for_update()
    )
    if connection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conexão não encontrada")

    now = datetime.now(UTC)
    if not force and connection.last_synced_at is not None:
        elapsed = now - connection.last_synced_at
        if elapsed < MIN_SYNC_INTERVAL:
            response.status_code = status.HTTP_409_CONFLICT
            return _sync_status(
                connection,
                throttled=True,
                retry_after_seconds=int((MIN_SYNC_INTERVAL - elapsed).total_seconds()),
            )

    # Gravado no início e não no fim: é o que faz o throttle sobreviver a um
    # reinício do processo — o lock em memória do runner não sobrevive.
    connection.last_synced_at = now
    await session.flush()

    started = runner.schedule_sync(
        tenant_id=tenant_id, connection_id=connection_id, client=client, full=full
    )
    if not started:
        # Já havia um sync desta conexão em andamento neste processo.
        logger.info("sync de %s já estava em andamento", connection_id)

    return _sync_status(connection)


@router.get("/{connection_id}/sync", response_model=SyncStatusRead)
async def get_sync_status(
    connection_id: uuid.UUID,
    session: AsyncSession = Depends(get_tenant_session),
) -> SyncStatusRead:
    """Estado do sync. É este o endpoint que a interface põe em polling."""
    return _sync_status(await _get_connection(session, connection_id))


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(
    connection_id: uuid.UUID,
    session: AsyncSession = Depends(get_tenant_session),
) -> None:
    """Desativa a conexão, sem apagar o histórico.

    Soft delete e não `DELETE`: a FK de `accounts` é `ON DELETE CASCADE`, então
    apagar de verdade levaria contas e transações junto — meses de extrato
    embora, sem volta, por causa de um clique.
    """
    await _get_connection(session, connection_id)
    await session.execute(
        update(BankConnection)
        .where(BankConnection.id == connection_id)
        .values(deleted_at=datetime.now(UTC))
    )
