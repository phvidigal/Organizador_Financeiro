"""Resumo do dashboard: soma por `kind`, quebra por categoria e série mensal.

Três agregações numa chamada, porque as três respondem à mesma pergunta e pedi-las
separadas faria a tela pintar em três tempos com recortes que podem divergir.

Este módulo só consulta e monta. A aritmética — inclusive a regra de `TRANSFER`
ficar fora do saldo — vive em `app/services/dashboard.py`, que não conhece o banco
e por isso pode ser testada sem ele.

O que **não** acontece: soma no cliente. `frontend/src/lib/api.ts` diz isso
explicitamente, e a razão é o `NUMERIC(18,2)` — `JSON.parse` transformaria o valor
em double e 0,10 voltaria a não ser representável.
"""

import uuid
from datetime import date as date_type

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import get_tenant_session
from app.models.account import Account
from app.models.enums import AccountType, CategorizationStatus, TransactionKind
from app.models.transaction import Transaction
from app.schemas.categorization import QueueCountsRead
from app.schemas.dashboard import DashboardSummary
from app.services import dashboard as agg
from app.services.categorization.catalog import load_catalog

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=DashboardSummary)
async def summary(
    date_from: date_type | None = Query(None),
    date_to: date_type | None = Query(None),
    account_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_tenant_session),
) -> DashboardSummary:
    """Somas do período, por `kind`, por categoria e por mês.

    `TRANSFER` fica **fora** de `net` e tem bloco próprio. Sem essa separação,
    aplicar R$ 5.000 num CDB apareceria como R$ 5.000 de gasto, e o resgate dos
    mesmos R$ 5.000 como receita meses depois — o mesmo dinheiro contado duas vezes.

    Os nomes dos parâmetros são os mesmos de `GET /transactions` para que a quebra
    por categoria linke direto para o extrato já filtrado.
    """
    default_from, default_to = agg.default_period(date_type.today())
    date_from = date_from or default_from
    date_to = date_to or default_to

    # `date_from`/`date_to` chegam como `datetime.date` de verdade — o FastAPI já
    # converteu. Passar a string crua num bind do asyncpg levantaria
    # `AttributeError: 'str' object has no attribute 'toordinal'`; o psycopg
    # aceitaria, o asyncpg não.
    filters = [
        Transaction.deleted_at.is_(None),
        Transaction.date >= date_from,
        Transaction.date <= date_to,
    ]
    if account_id is not None:
        filters.append(Transaction.account_id == account_id)

    def scoped(stmt: Select) -> Select:
        return stmt.where(*filters)

    # (1) kind x status — alimenta os três blocos e a fila de uma vez só.
    by_kind_status = (
        await session.execute(
            scoped(
                select(
                    Transaction.kind,
                    Transaction.categorization_status,
                    func.sum(Transaction.amount),
                    func.count(),
                )
            ).group_by(Transaction.kind, Transaction.categorization_status)
        )
    ).all()

    # (2) categoria x kind. O `FILTER` evita uma segunda varredura só para contar as
    # que aguardam revisão.
    needs_review = Transaction.categorization_status == CategorizationStatus.NEEDS_REVIEW.value
    by_category_rows = (
        await session.execute(
            scoped(
                select(
                    Transaction.category_id,
                    Transaction.kind,
                    func.sum(Transaction.amount),
                    func.count(),
                    func.count().filter(needs_review),
                )
            ).group_by(Transaction.category_id, Transaction.kind)
        )
    ).all()

    # (3) série mensal. `to_char` em vez de `date_trunc`: devolve 'YYYY-MM' direto,
    # que ordena lexicograficamente na ordem cronológica e não passa por timestamp.
    month = func.to_char(Transaction.date, "YYYY-MM")
    by_month_rows = (
        await session.execute(
            scoped(select(month, Transaction.kind, func.sum(Transaction.amount)))
            .group_by(month, Transaction.kind)
            .order_by(month)
        )
    ).all()

    # (4) saldo atual. **Fora do recorte de período**, de propósito: é uma
    # fotografia do que a Pluggy reporta hoje, não um fluxo. Só contas `BANK` —
    # o "saldo" de um cartão é dívida, e somá-lo a dinheiro em conta daria um
    # número sem significado.
    balance_filters = [
        Account.deleted_at.is_(None),
        Account.type == AccountType.BANK.value,
    ]
    if account_id is not None:
        balance_filters.append(Account.id == account_id)
    current_balance = await session.scalar(
        select(func.sum(Account.balance)).where(*balance_filters)
    )

    catalog = await load_catalog(session)

    income = agg.kind_total(by_kind_status, TransactionKind.INCOME)
    expense = agg.kind_total(by_kind_status, TransactionKind.EXPENSE)
    transfer = agg.kind_total(by_kind_status, TransactionKind.TRANSFER)
    counts = agg.queue_counts(by_kind_status)

    return DashboardSummary(
        date_from=date_from,
        date_to=date_to,
        income=income,
        expense=expense,
        transfer=transfer,
        # `transfer` fora da conta, e é o ponto inteiro do campo `kind`. Despesa já
        # é negativa, então é uma soma e não uma subtração. **Isto não é saldo** —
        # ver o comentário do campo em `app/schemas/dashboard.py`.
        net=income.total + expense.total,
        current_balance=current_balance,
        by_category=agg.by_category(by_category_rows, catalog),
        by_month=agg.by_month(by_month_rows),
        queue=QueueCountsRead(
            pending=counts.get(CategorizationStatus.PENDING.value, 0),
            categorized=counts.get(CategorizationStatus.CATEGORIZED.value, 0),
            needs_review=counts.get(CategorizationStatus.NEEDS_REVIEW.value, 0),
            failed=counts.get(CategorizationStatus.FAILED.value, 0),
        ),
    )
