"""A agregação do dashboard — o único lugar do sistema que soma dinheiro.

Três regras que não estão em constraint nenhuma, e que um `SUM(amount)` ingênuo
quebra em silêncio:

1. **`TRANSFER` fica fora de receita e despesa.** É a razão de `kind` existir:
   pagamento de fatura, Pix entre contas próprias e aplicação em investimento
   são dinheiro mudando de lugar. Somá-los contaria os mesmos reais duas vezes —
   na compra do cartão e no pagamento da fatura.
2. **A soma é com sinal, por `kind`, e não por sinal do valor.** Um estorno
   categorizado como despesa é `EXPENSE` com valor positivo e *abate* o gasto.
   Separar "entradas" e "saídas" pelo sinal o contaria como receita.
3. **O que não foi decidido entra no total, mas é declarado.** Linha `PENDING`,
   `NEEDS_REVIEW` ou `FAILED` carrega um `kind` provisório — derivado do sinal,
   ou o palpite do LLM que pediu revisão. Tirá-la do total esconderia gasto real
   (invariante 4); deixá-la sem aviso daria um número errado com cara de certo.
   Por isso cada total vem com a parcela `undecided` ao lado.

A montagem (`summarize`) é pura e separada da consulta (`load_summary`) pelo
mesmo motivo de `catalog.py`: é ela que precisa de teste, e testá-la não deveria
exigir banco.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import CategorizationStatus, TransactionKind
from app.models.transaction import Transaction
from app.services.categorization.catalog import LABEL_SEPARATOR, CategoryCatalog, load_catalog

# Moeda dos totais. Somar outra moeda junto daria um número sem unidade; as linhas
# que não são BRL ficam fora e são contadas em `other_currency_count`, para a tela
# avisar em vez de esconder.
BASE_CURRENCY = "BRL"

UNCATEGORIZED_LABEL = "Sem categoria"
INACTIVE_LABEL = "Categoria inativa"

ZERO = Decimal("0")


@dataclass(frozen=True)
class AggregateRow:
    """Uma célula do `GROUP BY` de `load_summary`. Sem ORM, de propósito."""

    currency_code: str
    kind: str
    category_id: uuid.UUID | None
    categorization_status: str
    # Soma dos valores positivos e dos negativos, separadas: o total é a soma das
    # duas, mas transferência só faz sentido mostrada como "entrou X, saiu Y".
    inflow: Decimal
    outflow: Decimal
    count: int


@dataclass
class KindTotal:
    """Totais de um `kind`. `total` é com sinal: despesa é negativa."""

    total: Decimal = ZERO
    inflow: Decimal = ZERO
    outflow: Decimal = ZERO
    count: int = 0
    # A parte do total que ainda não foi decidida (status diferente de
    # CATEGORIZED). Está **dentro** de `total`, não somada a ele.
    undecided_total: Decimal = ZERO
    undecided_count: int = 0


@dataclass
class CategoryTotal:
    category_id: uuid.UUID | None
    # Rótulo qualificado ("Alimentação > Delivery") e o nome da folha ("Delivery"):
    # a tela mostra a folha sob a raiz, mas o rótulo inteiro continua sendo o que
    # identifica a categoria sem ambiguidade.
    label: str
    name: str
    total: Decimal = ZERO
    count: int = 0
    undecided_count: int = 0


@dataclass
class RootTotal:
    """Uma raiz da taxonomia com as suas categorias.

    `root_id` é NULL nos dois baldes que não são categoria: "Sem categoria" e
    "Categoria inativa". O rótulo os distingue.
    """

    root_id: uuid.UUID | None
    label: str
    total: Decimal = ZERO
    count: int = 0
    undecided_count: int = 0
    children: list[CategoryTotal] = field(default_factory=list)


@dataclass
class UndecidedCounts:
    """Quantas linhas do período ainda esperam alguém — o LLM ou o titular.

    Só na moeda dos totais: são as linhas que a tela diz estarem dentro deles.
    """

    pending: int = 0
    needs_review: int = 0
    failed: int = 0


@dataclass
class Totals:
    income: KindTotal
    expense: KindTotal
    transfer: KindTotal
    # Receita mais despesa, com sinal. Transferência fica fora — regra 1.
    net: Decimal
    income_by_category: list[RootTotal]
    expense_by_category: list[RootTotal]
    transfer_by_category: list[RootTotal]
    undecided: UndecidedCounts
    other_currency_count: int


@dataclass
class DashboardSummary(Totals):
    date_from: date_type | None = None
    date_to: date_type | None = None
    account_id: uuid.UUID | None = None
    currency_code: str = BASE_CURRENCY
    # Data do lançamento mais recente, fora do filtro de período. É o que permite à
    # tela vazia apontar para onde há dado — o sync no tier pessoal depende da
    # coleta da Pluggy, e o mês corrente pode simplesmente não ter chegado ainda.
    latest_date: date_type | None = None


def _bucket(
    category_id: uuid.UUID | None, catalog: CategoryCatalog
) -> tuple[tuple[str, uuid.UUID | None], str, str, str]:
    """(chave da raiz, rótulo da raiz, rótulo da categoria, nome da folha)."""
    if category_id is None:
        return ("none", None), UNCATEGORIZED_LABEL, UNCATEGORIZED_LABEL, UNCATEGORIZED_LABEL

    entry = catalog.by_id.get(category_id)
    if entry is None:
        # O catálogo só tem as ativas, de propósito (ver `load_catalog`). Linha que
        # aponta para uma desativada continua contando — desativar não é apagar —,
        # mas sem nome inventado: mesma escolha das telas de revisão e extrato.
        return ("inactive", None), INACTIVE_LABEL, INACTIVE_LABEL.lower(), INACTIVE_LABEL.lower()

    root = catalog.by_id.get(entry.root_id)
    root_label = root.label if root is not None else entry.label
    name = entry.label.rsplit(LABEL_SEPARATOR, 1)[-1]
    return ("root", entry.root_id), root_label, entry.label, name


def _by_magnitude(total: Decimal, label: str) -> tuple[Decimal, str]:
    # Maior valor absoluto primeiro, independente do sinal: despesa é negativa, e
    # ordenar pelo valor cru poria o menor gasto no topo.
    return (-abs(total), label)


def _breakdown(rows: list[AggregateRow], catalog: CategoryCatalog) -> list[RootTotal]:
    roots: dict[tuple[str, uuid.UUID | None], RootTotal] = {}
    children: dict[tuple[str, uuid.UUID | None], dict[uuid.UUID | None, CategoryTotal]] = {}

    for row in rows:
        key, root_label, label, name = _bucket(row.category_id, catalog)
        amount = row.inflow + row.outflow
        decided = row.categorization_status == CategorizationStatus.CATEGORIZED
        undecided = 0 if decided else row.count

        root = roots.get(key)
        if root is None:
            root = roots[key] = RootTotal(root_id=key[1], label=root_label)
            children[key] = {}
        root.total += amount
        root.count += row.count
        root.undecided_count += undecided

        child = children[key].get(row.category_id)
        if child is None:
            child = children[key][row.category_id] = CategoryTotal(
                category_id=row.category_id, label=label, name=name
            )
        child.total += amount
        child.count += row.count
        child.undecided_count += undecided

    for key, root in roots.items():
        root.children = sorted(
            children[key].values(), key=lambda c: _by_magnitude(c.total, c.label)
        )
    return sorted(roots.values(), key=lambda r: _by_magnitude(r.total, r.label))


def summarize(rows: Iterable[AggregateRow], catalog: CategoryCatalog) -> Totals:
    """Monta os totais a partir das células agregadas. Puro."""
    kinds = {kind.value: KindTotal() for kind in TransactionKind}
    per_kind: dict[str, list[AggregateRow]] = {kind.value: [] for kind in TransactionKind}
    undecided = UndecidedCounts()
    other_currency_count = 0

    for row in rows:
        if row.currency_code != BASE_CURRENCY:
            other_currency_count += row.count
            continue

        # Depois do filtro de moeda, de propósito: a tela diz que as linhas sem
        # decisão "já estão nos totais", e a em dólar não está — ela tem aviso
        # próprio, por `other_currency_count`.
        if row.categorization_status == CategorizationStatus.PENDING:
            undecided.pending += row.count
        elif row.categorization_status == CategorizationStatus.NEEDS_REVIEW:
            undecided.needs_review += row.count
        elif row.categorization_status == CategorizationStatus.FAILED:
            undecided.failed += row.count

        total = kinds[row.kind]
        amount = row.inflow + row.outflow
        total.total += amount
        total.inflow += row.inflow
        total.outflow += row.outflow
        total.count += row.count
        if row.categorization_status != CategorizationStatus.CATEGORIZED:
            total.undecided_total += amount
            total.undecided_count += row.count
        per_kind[row.kind].append(row)

    income = kinds[TransactionKind.INCOME.value]
    expense = kinds[TransactionKind.EXPENSE.value]
    return Totals(
        income=income,
        expense=expense,
        transfer=kinds[TransactionKind.TRANSFER.value],
        net=income.total + expense.total,
        income_by_category=_breakdown(per_kind[TransactionKind.INCOME.value], catalog),
        expense_by_category=_breakdown(per_kind[TransactionKind.EXPENSE.value], catalog),
        transfer_by_category=_breakdown(per_kind[TransactionKind.TRANSFER.value], catalog),
        undecided=undecided,
        other_currency_count=other_currency_count,
    )


async def load_summary(
    session: AsyncSession,
    *,
    date_from: date_type,
    date_to: date_type,
    account_id: uuid.UUID | None = None,
) -> DashboardSummary:
    """Totais do período para o tenant corrente. O escopo vem do RLS.

    Uma consulta agrupada por (moeda, kind, categoria, status) e o resto em
    Python: o resultado tem dezenas de células, não milhares de linhas, e montar a
    hierarquia em SQL não compraria nada além de uma query mais difícil de ler.
    """
    filters = [Transaction.deleted_at.is_(None)]
    if account_id is not None:
        filters.append(Transaction.account_id == account_id)

    in_period = [*filters, Transaction.date >= date_from, Transaction.date <= date_to]
    result = await session.execute(
        select(
            Transaction.currency_code,
            Transaction.kind,
            Transaction.category_id,
            Transaction.categorization_status,
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.amount > 0), ZERO),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.amount < 0), ZERO),
            func.count(),
        )
        .where(*in_period)
        .group_by(
            Transaction.currency_code,
            Transaction.kind,
            Transaction.category_id,
            Transaction.categorization_status,
        )
    )
    rows = [
        AggregateRow(
            currency_code=currency,
            kind=kind,
            category_id=category_id,
            categorization_status=status,
            inflow=inflow,
            outflow=outflow,
            count=count,
        )
        for currency, kind, category_id, status, inflow, outflow, count in result
    ]

    totals = summarize(rows, await load_catalog(session))
    latest_date = await session.scalar(select(func.max(Transaction.date)).where(*filters))

    return DashboardSummary(
        **vars(totals),
        date_from=date_from,
        date_to=date_to,
        account_id=account_id,
        latest_date=latest_date,
    )
