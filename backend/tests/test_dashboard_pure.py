"""Aritmética do dashboard, sem banco e sem event loop.

Módulo síncrono de propósito: não pode ganhar `pytestmark = asyncio(...)`, ou o
asyncpg passa a reclamar de loop em qualquer teste que entre aqui depois.

O que se testa aqui é o que o `EXPLAIN ANALYZE` não pega — a soma continua certa
quando os dados chegam torto: `kind` faltando no mês, categoria desativada, mês
sem transferência, fevereiro de ano bissexto.
"""

import uuid
from datetime import date
from decimal import Decimal

from app.models.enums import TransactionKind
from app.services.categorization.catalog import CategoryRow, build_catalog
from app.services.dashboard import (
    INACTIVE_CATEGORY,
    NO_CATEGORY,
    by_category,
    by_month,
    default_period,
    kind_total,
    queue_counts,
)

# Linhas no formato que a consulta (1) devolve: (kind, status, soma, contagem).
KIND_ROWS = [
    ("INCOME", "CATEGORIZED", Decimal("1000.00"), 1),
    ("INCOME", "NEEDS_REVIEW", Decimal("200.00"), 1),
    ("EXPENSE", "CATEGORIZED", Decimal("-300.00"), 1),
    ("EXPENSE", "NEEDS_REVIEW", Decimal("-100.00"), 1),
    ("TRANSFER", "CATEGORIZED", Decimal("-500.00"), 1),
]

ID_ALIMENTACAO = uuid.UUID("11111111-1111-1111-1111-111111111111")
ID_DELIVERY = uuid.UUID("22222222-2222-2222-2222-222222222222")
# Não está no catálogo: é o caso da categoria desativada depois de já ter sido
# gravada em transações.
ID_DESATIVADA = uuid.UUID("33333333-3333-3333-3333-333333333333")


def catalogo():
    """Dois níveis, para o rótulo qualificado ter o que qualificar."""
    return build_catalog(
        [
            CategoryRow(id=ID_ALIMENTACAO, parent_id=None, name="Alimentação", kind="EXPENSE"),
            CategoryRow(id=ID_DELIVERY, parent_id=ID_ALIMENTACAO, name="Delivery", kind="EXPENSE"),
        ]
    )


# ---------------------------------------------------------------------------
# Período padrão
# ---------------------------------------------------------------------------


def test_periodo_padrao_alinha_no_primeiro_dia_do_mes() -> None:
    """Doze meses cheios contando o corrente, para a série não abrir pela metade."""
    inicio, fim = default_period(date(2026, 8, 14))
    assert inicio == date(2025, 9, 1)
    assert fim == date(2026, 8, 14)


def test_periodo_padrao_sobrevive_a_29_de_fevereiro() -> None:
    """`date(ano - 1, mês, dia)` estouraria aqui — daí a aritmética por índice de
    meses. Um dashboard que quebra num dia por quadriênio só aparece em produção."""
    inicio, _ = default_period(date(2024, 2, 29))
    assert inicio == date(2023, 3, 1)


def test_periodo_padrao_atravessa_a_virada_do_ano() -> None:
    inicio, _ = default_period(date(2026, 1, 5))
    assert inicio == date(2025, 2, 1)


# ---------------------------------------------------------------------------
# Somas por kind
# ---------------------------------------------------------------------------


def test_kind_total_separa_a_fatia_sob_revisao() -> None:
    receita = kind_total(KIND_ROWS, TransactionKind.INCOME)
    assert receita.total == Decimal("1200.00")
    assert receita.count == 2
    assert receita.needs_review_total == Decimal("200.00")
    assert receita.needs_review_count == 1


def test_kind_ausente_devolve_zero_e_nao_estoura() -> None:
    """Período sem nenhuma transferência é comum, e a tela precisa de um número."""
    vazio = kind_total([], TransactionKind.TRANSFER)
    assert vazio.total == Decimal("0.00")
    assert vazio.count == 0
    assert vazio.needs_review_total == Decimal("0.00")


def test_transferencia_nao_contamina_receita_nem_despesa() -> None:
    """A regressão silenciosa desta tela: os R$ 500 de transferência somados à
    despesa dariam −R$ 900, um número plausível e errado."""
    receita = kind_total(KIND_ROWS, TransactionKind.INCOME)
    despesa = kind_total(KIND_ROWS, TransactionKind.EXPENSE)
    transferencia = kind_total(KIND_ROWS, TransactionKind.TRANSFER)

    assert receita.total + despesa.total == Decimal("800.00")
    assert transferencia.total == Decimal("-500.00")


def test_queue_counts_agrega_os_status_de_todos_os_kind() -> None:
    counts = queue_counts(KIND_ROWS)
    assert counts["CATEGORIZED"] == 3
    assert counts["NEEDS_REVIEW"] == 2


# ---------------------------------------------------------------------------
# Quebra por categoria
# ---------------------------------------------------------------------------


def test_rotulo_vem_qualificado_do_catalogo() -> None:
    """O mesmo rótulo que o LLM vê no `enum` do schema — uma fonte, três leituras."""
    linhas = [(ID_DELIVERY, "EXPENSE", Decimal("-80.00"), 2, 0)]
    (linha,) = by_category(linhas, catalogo())
    assert linha.label == "Alimentação > Delivery"


def test_sem_categoria_e_categoria_inativa_sao_baldes_diferentes() -> None:
    """Confundi-los esconderia a diferença entre "ninguém classificou" e "aponta
    para uma categoria que o titular desativou"."""
    linhas = [
        (None, "EXPENSE", Decimal("-100.00"), 1, 1),
        (ID_DESATIVADA, "EXPENSE", Decimal("-50.00"), 1, 0),
    ]
    rotulos = {linha.label for linha in by_category(linhas, catalogo())}
    assert rotulos == {NO_CATEGORY, INACTIVE_CATEGORY}


def test_quebra_ordena_pelo_valor_absoluto() -> None:
    """Despesa é negativa e receita positiva: ordenar pelo número cru jogaria o
    maior gasto para o fim da lista."""
    linhas = [
        (ID_DELIVERY, "EXPENSE", Decimal("-800.00"), 3, 0),
        (ID_ALIMENTACAO, "INCOME", Decimal("100.00"), 1, 0),
        (None, "EXPENSE", Decimal("-2000.00"), 5, 0),
    ]
    pesos = [linha.total for linha in by_category(linhas, catalogo())]
    assert pesos == [Decimal("-2000.00"), Decimal("-800.00"), Decimal("100.00")]


# ---------------------------------------------------------------------------
# Série mensal
# ---------------------------------------------------------------------------


def test_mes_sem_um_dos_kind_devolve_zero_e_nao_ausencia() -> None:
    """Zero é um fato ("não houve transferência"); ausência viraria buraco no
    gráfico, que o cliente teria de adivinhar como preencher."""
    linhas = [
        ("2026-06", "INCOME", Decimal("1000.00")),
        ("2026-06", "EXPENSE", Decimal("-300.00")),
        ("2026-07", "INCOME", Decimal("200.00")),
    ]
    meses = {m.month: m for m in by_month(linhas)}
    assert meses["2026-06"].transfer == Decimal("0.00")
    assert meses["2026-07"].expense == Decimal("0.00")


def test_serie_sai_em_ordem_cronologica_atravessando_o_ano() -> None:
    """`YYYY-MM` ordena lexicograficamente na ordem certa — é por isso que a
    consulta usa `to_char` em vez de devolver um timestamp."""
    linhas = [
        ("2026-01", "INCOME", Decimal("1.00")),
        ("2025-12", "INCOME", Decimal("2.00")),
        ("2025-02", "INCOME", Decimal("3.00")),
    ]
    assert [m.month for m in by_month(linhas)] == ["2025-02", "2025-12", "2026-01"]


def test_meses_sem_lancamento_nenhum_nao_viram_balde() -> None:
    """Fabricar os vazios aqui obrigaria esta função a conhecer o calendário do
    período filtrado, que ela não recebe."""
    linhas = [
        ("2026-01", "INCOME", Decimal("1.00")),
        ("2026-04", "INCOME", Decimal("1.00")),
    ]
    assert [m.month for m in by_month(linhas)] == ["2026-01", "2026-04"]
