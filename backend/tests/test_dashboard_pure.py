"""Montagem dos totais do dashboard — síncrono e sem banco.

Módulo **sem** `pytestmark = pytest.mark.asyncio`: nada aqui é assíncrono (ver
CLAUDE.md). A consulta e o RLS estão em `test_api_dashboard.py`; aqui fica a
aritmética, que é onde um total errado com cara de certo nasceria.
"""

import uuid
from decimal import Decimal

from app.services.categorization.catalog import CategoryRow, build_catalog
from app.services.dashboard import (
    INACTIVE_LABEL,
    UNCATEGORIZED_LABEL,
    AggregateRow,
    summarize,
)

ALIMENTACAO = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
DELIVERY = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
SUPERMERCADO = uuid.UUID("00000000-0000-0000-0000-0000000000a3")
TRANSPORTE = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
RECEITAS = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
SALARIO = uuid.UUID("00000000-0000-0000-0000-0000000000d2")
TRANSFERENCIAS = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
PAGAMENTO_CARTAO = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
DESATIVADA = uuid.UUID("00000000-0000-0000-0000-0000000000e1")


def catalog():
    # `DESATIVADA` fica de fora de propósito: `load_catalog` só carrega as ativas.
    return build_catalog(
        [
            CategoryRow(ALIMENTACAO, None, "Alimentação", "EXPENSE"),
            CategoryRow(DELIVERY, ALIMENTACAO, "Delivery", "EXPENSE"),
            CategoryRow(SUPERMERCADO, ALIMENTACAO, "Supermercado", "EXPENSE"),
            CategoryRow(TRANSPORTE, None, "Transporte", "EXPENSE"),
            CategoryRow(RECEITAS, None, "Receitas", "INCOME"),
            CategoryRow(SALARIO, RECEITAS, "Salário", "INCOME"),
            CategoryRow(TRANSFERENCIAS, None, "Transferências", "TRANSFER"),
            CategoryRow(PAGAMENTO_CARTAO, TRANSFERENCIAS, "Pagamento de cartão", "TRANSFER"),
        ]
    )


def row(
    kind,
    category_id,
    *,
    inflow="0",
    outflow="0",
    count=1,
    status="CATEGORIZED",
    currency="BRL",
):
    return AggregateRow(
        currency_code=currency,
        kind=kind,
        category_id=category_id,
        categorization_status=status,
        inflow=Decimal(inflow),
        outflow=Decimal(outflow),
        count=count,
    )


def test_transfer_stays_out_of_income_expense_and_net():
    """A regra que justifica o campo `kind`: o pagamento da fatura não é gasto."""
    totals = summarize(
        [
            row("INCOME", SALARIO, inflow="5000.00"),
            row("EXPENSE", DELIVERY, outflow="-300.00"),
            row("TRANSFER", PAGAMENTO_CARTAO, outflow="-1200.00"),
            row("TRANSFER", PAGAMENTO_CARTAO, inflow="1200.00"),
        ],
        catalog(),
    )

    assert totals.income.total == Decimal("5000.00")
    assert totals.expense.total == Decimal("-300.00")
    assert totals.net == Decimal("4700.00")
    assert totals.transfer.inflow == Decimal("1200.00")
    assert totals.transfer.outflow == Decimal("-1200.00")
    assert totals.transfer.count == 2


def test_refund_categorized_as_expense_reduces_spending():
    """Estorno é EXPENSE com valor positivo: abate o gasto, não vira receita."""
    totals = summarize(
        [
            row("EXPENSE", DELIVERY, outflow="-100.00"),
            row("EXPENSE", DELIVERY, inflow="40.00"),
        ],
        catalog(),
    )

    assert totals.expense.total == Decimal("-60.00")
    assert totals.expense.inflow == Decimal("40.00")
    assert totals.income.total == Decimal("0")
    assert totals.net == Decimal("-60.00")


def test_undecided_rows_count_in_total_and_are_declared():
    """Fora do total esconderia gasto; dentro sem aviso seria número com cara de certo."""
    totals = summarize(
        [
            row("EXPENSE", DELIVERY, outflow="-100.00"),
            row("EXPENSE", DELIVERY, outflow="-30.00", status="NEEDS_REVIEW"),
            # PENDING ainda não tem categoria e o `kind` saiu do sinal — é como uma
            # transferência não classificada conta como gasto (invariante 4).
            row("EXPENSE", None, outflow="-20.00", status="PENDING"),
            row("INCOME", None, inflow="10.00", status="FAILED"),
        ],
        catalog(),
    )

    assert totals.expense.total == Decimal("-150.00")
    assert totals.expense.undecided_total == Decimal("-50.00")
    assert totals.expense.undecided_count == 2
    assert totals.income.undecided_total == Decimal("10.00")
    assert (totals.undecided.pending, totals.undecided.needs_review, totals.undecided.failed) == (
        1,
        1,
        1,
    )


def test_breakdown_rolls_children_up_to_root():
    totals = summarize(
        [
            row("EXPENSE", DELIVERY, outflow="-100.00", count=3),
            row("EXPENSE", SUPERMERCADO, outflow="-250.00", count=2),
            # Lançamento na própria raiz: vira um filho com o nome dela.
            row("EXPENSE", ALIMENTACAO, outflow="-10.00"),
            row("EXPENSE", TRANSPORTE, outflow="-80.00"),
        ],
        catalog(),
    )

    alimentacao, transporte = totals.expense_by_category
    assert alimentacao.root_id == ALIMENTACAO
    assert alimentacao.label == "Alimentação"
    assert alimentacao.total == Decimal("-360.00")
    assert alimentacao.count == 6
    assert [c.name for c in alimentacao.children] == ["Supermercado", "Delivery", "Alimentação"]
    assert alimentacao.children[0].label == "Alimentação > Supermercado"
    assert transporte.total == Decimal("-80.00")


def test_breakdown_orders_by_magnitude_not_by_signed_value():
    """Despesa é negativa: ordenar pelo valor cru poria o menor gasto no topo."""
    totals = summarize(
        [
            row("EXPENSE", TRANSPORTE, outflow="-10.00"),
            row("EXPENSE", DELIVERY, outflow="-500.00"),
        ],
        catalog(),
    )

    assert [r.label for r in totals.expense_by_category] == ["Alimentação", "Transporte"]


def test_breakdown_is_split_by_kind_not_by_category_kind():
    """O titular pode sobrepor o `kind`: um Pix numa categoria TRANSFER marcado como
    despesa entra nos gastos, sob a categoria que tem."""
    totals = summarize(
        [row("EXPENSE", PAGAMENTO_CARTAO, outflow="-90.00")],
        catalog(),
    )

    assert totals.expense.total == Decimal("-90.00")
    assert totals.transfer.count == 0
    assert totals.expense_by_category[0].label == "Transferências"


def test_uncategorized_and_inactive_get_their_own_buckets():
    totals = summarize(
        [
            row("EXPENSE", None, outflow="-40.00", status="PENDING"),
            row("EXPENSE", DESATIVADA, outflow="-15.00"),
        ],
        catalog(),
    )

    sem, inativa = totals.expense_by_category
    assert (sem.root_id, sem.label, sem.undecided_count) == (None, UNCATEGORIZED_LABEL, 1)
    assert sem.children[0].category_id is None
    assert (inativa.root_id, inativa.label) == (None, INACTIVE_LABEL)
    # O id continua lá: é o que deixa a tela abrir o extrato filtrado por ela.
    assert inativa.children[0].category_id == DESATIVADA


def test_other_currency_stays_out_of_totals_but_is_counted():
    totals = summarize(
        [
            row("EXPENSE", DELIVERY, outflow="-100.00"),
            row("EXPENSE", DELIVERY, outflow="-25.00", currency="USD", status="NEEDS_REVIEW"),
        ],
        catalog(),
    )

    assert totals.expense.total == Decimal("-100.00")
    assert totals.other_currency_count == 1
    # Nem nos contadores de pendência: a tela diz que eles "já estão nos totais".
    assert totals.undecided.needs_review == 0
    assert totals.expense_by_category[0].total == Decimal("-100.00")


def test_empty_period():
    totals = summarize([], catalog())

    assert totals.net == Decimal("0")
    assert totals.expense_by_category == []
    assert totals.income.count == 0
