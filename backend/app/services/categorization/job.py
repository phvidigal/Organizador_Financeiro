"""Orquestração da categorização.

Duas regras atravessam o módulo, e são as mesmas de `app/services/pluggy/sync.py`:

**Nenhuma chamada ao Ollama acontece com transação de banco aberta.** Uma geração
leva segundos; segurar a transação durante isso deixaria a conexão `idle in
transaction` por minutos a fio. O padrão é sempre: fecha a sessão, chama o Ollama,
reabre para gravar.

**Uma unidade de trabalho por transação.** É o que torna o job retomável de graça:
a fila é definida por `categorization_status`, cada linha é gravada e comitada
sozinha, e uma queda no meio do backlog preserva tudo que já foi feito. Um lote
único numa transação só desfaria horas de GPU no primeiro erro.

Concorrência é **1**, deliberadamente. O Ollama serializa requisições por padrão:
paralelismo alto não acelera, só enfileira — e ainda tira a serialização do lugar
onde ela é observável.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import TenantSessionScope, tenant_session
from app.models.account import Account
from app.models.enums import CategorizationStatus
from app.models.transaction import Transaction
from app.services.categorization.catalog import (
    OWN_TRANSFER_LABEL,
    CategoryCatalog,
    load_catalog,
)
from app.services.categorization.client import OllamaClient
from app.services.categorization.decide import Decision, decide
from app.services.categorization.errors import (
    OllamaResponseError,
    OllamaUnavailableError,
)
from app.services.categorization.prompt import (
    TransactionForPrompt,
    build_messages,
    build_schema,
    describe_counterparty,
    parse_response,
)
from app.services.categorization.rules import apply_rules, load_active_rules
from app.services.categorization.store import apply_decision, apply_own_transfer_evidence

logger = logging.getLogger(__name__)

# Linhas lidas por vez. Não é tamanho de lote de escrita — cada transação é gravada
# sozinha; é só para não trazer 300 linhas de uma vez numa consulta que vai ser
# refeita de qualquer jeito conforme a fila encolhe.
_PAGE_SIZE = 50

# Falhas de infraestrutura seguidas antes de desistir da execução.
#
# Três e não uma porque o `OllamaUnavailableError` já chega depois de três
# tentativas internas com backoff: chegar aqui três vezes seguidas significa nove
# tentativas frustradas, e nesse ponto insistir é só gastar tempo.
_MAX_CONSECUTIVE_FAILURES = 3

CategorizationStatusLiteral = Literal["SUCCESS", "PARTIAL", "FAILED"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class CategorizationOutcome:
    """Resumo de uma execução. Vive em memória; o durável está em `transactions`."""

    tenant_id: uuid.UUID
    started_at: datetime
    status: CategorizationStatusLiteral = "SUCCESS"
    finished_at: datetime | None = None
    model: str | None = None
    processed: int = 0
    categorized: int = 0
    needs_review: int = 0
    failed: int = 0
    # Decididas antes do LLM, sem gastar GPU: pela evidência de conta própria ou
    # por uma regra do titular. Não entram em `processed`, que conta chamadas ao
    # modelo e é o que o `limit` limita.
    by_rule: int = 0
    # Linhas que a regra de conta própria tinha decidido e voltaram para a fila
    # porque a evidência sumiu.
    returned_to_queue: int = 0
    error: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)


async def categorize_pending(
    *,
    tenant_id: uuid.UUID,
    client: OllamaClient,
    session_scope: TenantSessionScope = tenant_session,
    limit: int | None = None,
    now: Callable[[], datetime] = _utcnow,
) -> CategorizationOutcome:
    """Consome a fila `PENDING` do tenant, uma transação por chamada ao Ollama.

    `session_scope` e `now` são injetáveis pelo mesmo motivo do sync: `tenant_session`
    está preso ao `SessionLocal` de produção, e o teste precisa do banco de teste.

    Nunca levanta. Quem chama é uma task de background, e exceção que escapa dali só
    aparece como "Task exception was never retrieved", possivelmente minutos depois
    e sem contexto.
    """
    outcome = CategorizationOutcome(
        tenant_id=tenant_id, started_at=now(), model=client.model
    )
    phase = "carregar catálogo"

    try:
        async with session_scope(tenant_id) as session:
            catalog = await load_catalog(session)

        if not catalog:
            # Tenant sem taxonomia. Sem categorias não há `enum`, e um schema com
            # `enum: []` não restringe nada — o modelo responderia qualquer coisa.
            outcome.status = "PARTIAL"
            outcome.warnings.append("tenant sem categorias ativas; nada a categorizar")
            outcome.finished_at = now()
            return outcome

        schema = build_schema(catalog.labels)

        # Antes do LLM, o que não precisa dele. Numa unidade de trabalho própria e
        # sem HTTP, como o resto do job.
        phase = "aplicar regras"
        async with session_scope(tenant_id) as session:
            await _apply_rules(session, catalog=catalog, outcome=outcome, now=now())

        # Linhas que continuam PENDING de propósito (Ollama fora do ar no meio da
        # execução). Sem excluí-las da consulta, a próxima página traria as mesmas.
        stuck_ids: set[uuid.UUID] = set()
        # Fusível contra laço infinito: se uma escrita não pegar, a mesma linha
        # voltaria na próxima página e o job giraria para sempre consumindo GPU.
        attempted_ids: set[uuid.UUID] = set()
        consecutive_failures = 0

        while limit is None or outcome.processed < limit:
            phase = "ler a fila"
            remaining = None if limit is None else limit - outcome.processed
            page_size = _PAGE_SIZE if remaining is None else min(_PAGE_SIZE, remaining)

            async with session_scope(tenant_id) as session:
                page = await _next_page(session, page_size=page_size, skip=stuck_ids)

            if not page:
                break

            if attempted_ids.issuperset(tx.id for tx in page):
                # A página inteira já passou por aqui e voltou PENDING: a gravação
                # não está pegando. Parar com aviso é melhor que girar em silêncio.
                outcome.status = "PARTIAL"
                outcome.warnings.append(
                    "a fila devolveu apenas transações já processadas; execução interrompida"
                )
                logger.error("fila de categorização não avança; interrompendo")
                break

            for tx in page:
                attempted_ids.add(tx.id)
                phase = f"categorizar {tx.id}"

                try:
                    # Sem sessão aberta: é a regra do módulo.
                    content = await client.chat(
                        messages=build_messages(tx, catalog), format_schema=schema
                    )
                    decision = decide(
                        parse_response(content),
                        catalog=catalog,
                        pluggy_category_id=tx.pluggy_category_id,
                        counterparty_is_holder=tx.counterparty_is_holder,
                    )
                    consecutive_failures = 0

                except OllamaUnavailableError as exc:
                    # Infraestrutura. A linha fica PENDING — marcar 333 transações
                    # como FAILED porque o Ollama estava desligado exigiria um reset
                    # manual para recuperar, que é o oposto de falhar seguro.
                    consecutive_failures += 1
                    stuck_ids.add(tx.id)
                    logger.warning(
                        "Ollama indisponível ao categorizar %s (%s/%s): %s",
                        tx.id,
                        consecutive_failures,
                        _MAX_CONSECUTIVE_FAILURES,
                        exc,
                    )
                    if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                        outcome.status = "FAILED"
                        outcome.error = {
                            "phase": phase,
                            "type": type(exc).__name__,
                            "message": str(exc)[:500],
                            "at": now().isoformat(),
                        }
                        outcome.finished_at = now()
                        return outcome
                    continue

                except OllamaResponseError as exc:
                    # Houve resposta, ela é que não serve. Problema daquela linha:
                    # vai para revisão humana, e o laço segue.
                    logger.warning("resposta inutilizável para %s: %s", tx.id, exc)
                    decision = Decision(
                        status=CategorizationStatus.NEEDS_REVIEW,
                        reason=str(exc)[:200],
                    )
                    consecutive_failures = 0

                except Exception as exc:  # noqa: BLE001 - uma linha ruim não derruba o job
                    logger.exception("erro inesperado ao categorizar %s", tx.id)
                    decision = Decision(status=CategorizationStatus.FAILED, reason=str(exc)[:200])
                    consecutive_failures = 0

                phase = f"gravar {tx.id}"
                async with session_scope(tenant_id) as session:
                    await apply_decision(
                        session, transaction_id=tx.id, decision=decision, now=now()
                    )

                outcome.processed += 1
                _count(outcome, decision)
                if decision.reason:
                    logger.info("transação %s -> %s (%s)", tx.id, decision.status, decision.reason)

                if limit is not None and outcome.processed >= limit:
                    break

        if outcome.failed:
            outcome.status = "PARTIAL"
        outcome.finished_at = now()
        return outcome

    except Exception as exc:  # noqa: BLE001 - a task de background não pode explodir
        logger.exception("categorização do tenant %s falhou em '%s'", tenant_id, phase)
        outcome.status = "FAILED"
        outcome.error = {
            "phase": phase,
            "type": type(exc).__name__,
            "message": str(exc)[:500],
            "at": now().isoformat(),
        }
        outcome.finished_at = now()
        return outcome


async def _apply_rules(
    session: AsyncSession,
    *,
    catalog: CategoryCatalog,
    outcome: CategorizationOutcome,
    now: datetime,
) -> None:
    """O passo de regras, na ordem da precedência.

    1. **Evidência de conta própria**, sobre o histórico inteiro e não só a fila:
       é ela que corrige o `Pix recebido` do próprio titular que o LLM mandou para
       Receitas antes de a evidência existir. Devolve à fila o que perdeu a evidência.
    2. **Regras do titular**, só sobre o que ainda não tem decisão. A regra nova já
       foi aplicada ao histórico quando foi criada (`PATCH /transactions/{id}`).

    O que sobrar `PENDING` segue para o LLM.
    """
    own = catalog.own_transfer
    if own is None:
        outcome.warnings.append(
            f"categoria {OWN_TRANSFER_LABEL!r} não encontrada; "
            "transferência entre contas próprias não será reconhecida"
        )
    else:
        promoted, demoted = await apply_own_transfer_evidence(session, category=own, now=now)
        outcome.by_rule += promoted
        outcome.returned_to_queue += demoted

    outcome.by_rule += await apply_rules(
        session, rules=await load_active_rules(session), catalog=catalog, include_llm=False, now=now
    )


def _count(outcome: CategorizationOutcome, decision: Decision) -> None:
    if decision.status == CategorizationStatus.CATEGORIZED:
        outcome.categorized += 1
    elif decision.status == CategorizationStatus.NEEDS_REVIEW:
        outcome.needs_review += 1
    elif decision.status == CategorizationStatus.FAILED:
        outcome.failed += 1


async def _next_page(
    session: AsyncSession, *, page_size: int, skip: set[uuid.UUID]
) -> list[TransactionForPrompt]:
    """Próxima página da fila, já no formato que o prompt consome.

    `ORDER BY date DESC` casa com `ix_transactions_pending`, que é parcial
    (`WHERE categorization_status = 'PENDING'`) e encolhe conforme o backlog é
    processado. O mais recente primeiro é o que o usuário vê primeiro na tela.

    O `LEFT JOIN` em `accounts` traz o `type`: num cartão de crédito, "PAGAMENTO"
    quase sempre é a fatura sendo quitada — contexto que muda a categoria.
    """
    stmt = (
        select(
            Transaction.id,
            Transaction.date,
            Transaction.amount,
            Transaction.currency_code,
            Transaction.description_raw,
            Transaction.merchant,
            Transaction.pluggy_category_id,
            Transaction.pluggy_category_name,
            Transaction.counterparty_is_holder,
            # Só o bloco que sobreviveu à minimização: meio de pagamento e o tipo de
            # documento de cada ponta (`mappers.minimize_raw_payload`).
            Transaction.raw_payload["paymentData"].label("payment_data"),
            Account.type.label("account_type"),
        )
        .join(Account, Account.id == Transaction.account_id, isouter=True)
        .where(
            Transaction.categorization_status == CategorizationStatus.PENDING.value,
            Transaction.deleted_at.is_(None),
        )
        .order_by(Transaction.date.desc(), Transaction.created_at.desc())
        .limit(page_size)
    )
    if skip:
        stmt = stmt.where(Transaction.id.notin_(skip))

    result = await session.execute(stmt)
    return [
        TransactionForPrompt(
            id=row.id,
            date=row.date,
            amount=row.amount,
            currency_code=row.currency_code,
            description_raw=row.description_raw,
            merchant=row.merchant,
            pluggy_category_id=row.pluggy_category_id,
            pluggy_category_name=row.pluggy_category_name,
            account_type=row.account_type,
            counterparty=describe_counterparty(
                row.amount, row.counterparty_is_holder, row.payment_data
            ),
            counterparty_is_holder=row.counterparty_is_holder,
        )
        for row in result
    ]


__all__ = ["CategorizationOutcome", "categorize_pending"]
