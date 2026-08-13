"""Orquestração de uma sincronização com a Pluggy.

Regra que atravessa o módulo inteiro: **nenhuma chamada HTTP acontece com uma
transação de banco aberta.** Atualizar um item na Pluggy leva de segundos a
minutos; segurar a transação durante isso deixaria a conexão `idle in transaction`
e faria a falha da última página descartar tudo que já tinha sido gravado. O
padrão é sempre: fecha a sessão, chama a Pluggy, reabre para gravar.

Isso também é o que torna o sync retomável — uma queda na página 7 preserva as
seis primeiras, e o marco de watermark só avança no fim.
"""

import logging
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select, update

from app.core.tenancy import TenantSessionScope, tenant_session
from app.models.bank_connection import BankConnection
from app.models.enums import ConnectionStatus
from app.services.accounts import upsert_pluggy_accounts
from app.services.ingestion import upsert_external_transactions
from app.services.pluggy.category_map import CategoryMapReport, sync_category_map
from app.services.pluggy.client import PluggyClient
from app.services.pluggy.errors import PluggyError
from app.services.pluggy.mappers import map_account, map_item, map_transaction

logger = logging.getLogger(__name__)

# Folga sobre o início da execução anterior.
#
# O filtro é `createdAtFrom` e não `dateFrom`: uma transação antiga pode ser criada
# hoje na Pluggy, e filtrar pela data do lançamento a perderia.
#
# O marco é o `started_at` da execução anterior, não o `now()` do fim dela: uma
# transação criada na Pluggy *durante* a execução cairia num buraco entre "depois
# do meu início" e "antes do meu fim".
#
# A folga de um dia cobre defasagem de relógio entre nós e a Pluggy. Reler um dia
# custa zero — o upsert é idempotente por `(tenant, source, external_id)`.
_WATERMARK_SLACK = timedelta(days=1)

# Linhas por INSERT. 500 (o tamanho de página da Pluggy) × 16 colunas dá 8.000
# binds, dentro do teto de 32.767 do asyncpg — mas uma coluna nova em
# `transactions` numa fase futura empurraria isso para perto do limite. Uma linha
# de código agora remove a bomba-relógio.
_UPSERT_CHUNK = 200

# Pedir à Pluggy que atualize o item na instituição.
#
# 🔬 **Desligado por medição, não por preferência.** No tier pessoal, `PATCH
# /items/{id}` responde `400 "MeuPluggy item cant be updated"`: items do connector
# MeuPluggy não são atualizáveis por API. Quem dita a frequência é a Pluggy, que
# sincroniza sozinha a cada ~24h e anuncia a próxima em `nextAutoSyncAt`.
#
# Consequência para o produto: o nosso sync **lê** o que a Pluggy já coletou, e a
# interface não pode prometer "atualizar agora". Ver o README.
#
# Continua sendo um parâmetro, e não código removido, porque um plano pago com
# connector direto de instituição provavelmente aceita o PATCH — e aí é só passar
# `request_refresh=True`.
REQUEST_REFRESH_BY_DEFAULT = False

SyncStatus = Literal["SUCCESS", "PARTIAL", "FAILED"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class SyncOutcome:
    """Resumo de uma execução. Vive em memória; o que é durável está no banco."""

    connection_id: uuid.UUID
    # Redundante em relação à conexão, mas necessário: quem encadeia a
    # categorização é o `done_callback` do runner, que só recebe a task — e
    # descobrir o tenant dali exigiria abrir uma sessão de banco num callback
    # síncrono. Ver `pluggy/runner.py::_finalize`.
    tenant_id: uuid.UUID
    started_at: datetime
    status: SyncStatus = "SUCCESS"
    finished_at: datetime | None = None
    accounts_synced: int = 0
    transactions_upserted: int = 0
    # None = não tentado. False = a Pluggy recusou (provável limite do tier).
    item_refresh_supported: bool | None = None
    connection_status: str | None = None
    categories: CategoryMapReport | None = None
    error: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)


def _chunks(rows: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


async def sync_connection(
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    client: PluggyClient,
    session_scope: TenantSessionScope = tenant_session,
    full: bool = False,
    request_refresh: bool = REQUEST_REFRESH_BY_DEFAULT,
    now: Callable[[], datetime] = _utcnow,
) -> SyncOutcome:
    """Sincroniza uma conexão: item → contas → transações → de/para de categorias.

    `session_scope` e `now` são injetáveis porque o teste precisa dos dois:
    `tenant_session` está preso ao `SessionLocal` do banco de produção, e o
    watermark é lógica de tempo — testar tempo exige controlá-lo.

    Nunca levanta: falha vira `SyncOutcome(status="FAILED")` com o erro também
    gravado em `bank_connections.error`. Quem chama é uma task de background, e
    exceção que escapa dali só aparece como "Task exception was never retrieved".
    """
    started_at = now()
    outcome = SyncOutcome(
        connection_id=connection_id, tenant_id=tenant_id, started_at=started_at
    )
    phase = "carregar conexão"
    item_status_written = False

    try:
        # --- 0. Estado atual, e só isso: a sessão fecha antes do primeiro HTTP ---
        async with session_scope(tenant_id) as session:
            row = (
                await session.execute(
                    select(
                        BankConnection.pluggy_item_id, BankConnection.last_success_at
                    ).where(BankConnection.id == connection_id)
                )
            ).one_or_none()

        if row is None:
            # Sob RLS, "não existe" e "é de outro tenant" são indistinguíveis — e é
            # exatamente o modo de falha que se quer.
            raise LookupError(f"conexão {connection_id} não encontrada neste tenant")

        item_id, last_success_at = row

        # --- 1. Pedir atualização na origem -------------------------------------
        if request_refresh:
            phase = "PATCH /items"
            try:
                await client.refresh_item(item_id)
                outcome.item_refresh_supported = True
            except PluggyError as exc:
                # Pedir atualização é melhor-esforço, nunca pré-requisito. O 403 é
                # ⚠️ esperado no tier pessoal; um 5xx é a Pluggy instável. Nos dois
                # casos o que resta é ler o que ela já sincronizou por conta
                # própria, que é justamente o modo de operação se o tier gratuito
                # não permitir disparar atualização.
                #
                # Se a falha for de credencial, o `GET /items` logo abaixo falha
                # também — e aí sim o sync termina em FAILED, com a fase certa.
                outcome.item_refresh_supported = False
                outcome.warnings.append(
                    f"não foi possível pedir atualização do item: {type(exc).__name__}"
                )
                logger.info("PATCH /items não concluído para %s: %s", item_id, exc)

        # --- 2 e 3. Estado do item, gravado antes de qualquer conta -------------
        phase = "GET /items"
        item = await client.get_item(item_id)
        item_columns = map_item(item)

        async with session_scope(tenant_id) as session:
            await session.execute(
                update(BankConnection)
                .where(BankConnection.id == connection_id)
                .values(**item_columns, last_synced_at=started_at)
            )
        item_status_written = True
        outcome.connection_status = item_columns["status"]

        # --- 4. Credencial quebrada: parar aqui ---------------------------------
        if item_columns["status"] == ConnectionStatus.LOGIN_ERROR:
            # Ler contas agora devolveria o dado velho que já está no banco e
            # mascararia a única coisa que importa: o usuário precisa reconectar.
            outcome.status = "PARTIAL"
            outcome.warnings.append("conexão em LOGIN_ERROR; é preciso reconectar")
            outcome.finished_at = now()
            return outcome

        # --- 5. Contas -----------------------------------------------------------
        phase = "GET /accounts"
        accounts = await client.list_accounts(item_id)
        account_rows = []
        for account in accounts:
            try:
                account_rows.append(
                    map_account(
                        account, tenant_id=tenant_id, bank_connection_id=connection_id
                    )
                )
            except ValueError as exc:
                # Conta de tipo que não modelamos (investimento, empréstimo). Pular
                # uma conta é melhor que perder o sync das outras.
                outcome.warnings.append(str(exc))
                logger.warning("conta ignorada no item %s: %s", item_id, exc)

        async with session_scope(tenant_id) as session:
            saved = await upsert_pluggy_accounts(session, account_rows)
        outcome.accounts_synced = len(saved)

        # --- 6. Transações, uma transação de banco por página --------------------
        watermark = _watermark(last_success_at, full=full)
        for pluggy_account_id, (account_id, account_type) in saved.items():
            phase = f"GET /v2/transactions (conta {pluggy_account_id})"
            async for page in client.iter_transactions(
                pluggy_account_id, created_at_from=watermark
            ):
                rows = [
                    map_transaction(
                        tx,
                        tenant_id=tenant_id,
                        account_id=account_id,
                        account_type=account_type,
                    )
                    for tx in page
                ]
                for chunk in _chunks(rows, _UPSERT_CHUNK):
                    async with session_scope(tenant_id) as session:
                        # Sempre por `ingestion.py`: é ele que preserva categoria
                        # manual, ressuscita linha soft-deleted e deriva `kind`.
                        # Um INSERT próprio aqui passaria nos testes daquele módulo
                        # e quebraria as três regras em silêncio.
                        outcome.transactions_upserted += await upsert_external_transactions(
                            session, chunk
                        )

        # --- 7. De/para de categorias -------------------------------------------
        phase = "GET /categories"
        categories = await client.list_categories()
        async with session_scope(tenant_id) as session:
            outcome.categories = await sync_category_map(session, pluggy_categories=categories)

        # --- 8. Marco do incremental, só depois de tudo ter dado certo -----------
        async with session_scope(tenant_id) as session:
            await session.execute(
                update(BankConnection)
                .where(BankConnection.id == connection_id)
                .values(last_success_at=started_at, error=None)
            )

        outcome.finished_at = now()
        return outcome

    except Exception as exc:  # noqa: BLE001 - a task de background não pode explodir
        logger.exception("sync da conexão %s falhou em '%s'", connection_id, phase)
        outcome.status = "FAILED"
        outcome.error = {
            "phase": phase,
            "type": type(exc).__name__,
            "message": str(exc)[:500],
            "at": now().isoformat(),
        }
        outcome.finished_at = now()
        await _record_failure(
            session_scope,
            tenant_id=tenant_id,
            connection_id=connection_id,
            error=outcome.error,
            set_status_error=not item_status_written,
        )
        return outcome


def _watermark(last_success_at: datetime | None, *, full: bool) -> datetime | None:
    """Ponto de corte do sync incremental. `None` = histórico completo.

    Primeira execução (`last_success_at is None`) lê tudo — é o backfill inicial.
    `full=True` força o mesmo, e é o botão que reescreve o histórico depois de uma
    correção de convenção de sinal, sem migração de dados.
    """
    if full or last_success_at is None:
        return None
    return last_success_at - _WATERMARK_SLACK


async def _record_failure(
    session_scope: TenantSessionScope,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    error: dict[str, Any],
    set_status_error: bool,
) -> None:
    """Grava o erro numa sessão nova.

    Nova de propósito: a sessão que falhou pode estar em transação abortada, e
    qualquer comando nela levantaria um segundo erro por cima do primeiro.

    `status` só vira ERROR quando a falha é nossa (rede, 5xx, bug). Se a Pluggy já
    reportou o estado dela — `LOGIN_ERROR`, `WAITING_USER_INPUT` — esse estado é o
    que a interface precisa mostrar, e sobrescrevê-lo com um genérico ERROR
    trocaria "reconecte sua conta" por "algo deu errado".
    """
    values: dict[str, Any] = {"error": error}
    if set_status_error:
        values["status"] = ConnectionStatus.ERROR.value

    try:
        async with session_scope(tenant_id) as session:
            await session.execute(
                update(BankConnection)
                .where(BankConnection.id == connection_id)
                .values(**values)
            )
    except Exception:
        # O banco pode ser justamente o que caiu. Perder o registro do erro é ruim;
        # perder o log dele por causa de uma segunda exceção seria pior.
        logger.exception("não foi possível registrar a falha da conexão %s", connection_id)
