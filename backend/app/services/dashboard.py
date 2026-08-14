"""A parte do dashboard que não fala com o banco.

Separada de `app/api/v1/dashboard.py` pela mesma razão que `catalog.build_catalog`
é separada de `catalog.load_catalog`: a aritmética é o que precisa de teste, e
testá-la não deveria exigir Postgres. O endpoint executa três consultas agregadas e
entrega as linhas cruas para cá.

A regra que atravessa tudo: **`TRANSFER` nunca entra em `net`.** Aplicar num CDB,
pagar a fatura do cartão ou mandar um Pix para a própria conta move dinheiro sem ser
gasto nem receita. Somá-los infla as duas pontas e conta o mesmo dinheiro duas
vezes — que é exatamente o que o campo `kind` existe para evitar.
"""

from collections.abc import Sequence
from datetime import date as date_type
from decimal import Decimal
from typing import Any

from app.models.enums import CategorizationStatus, TransactionKind
from app.schemas.dashboard import CategoryTotal, KindTotal, MonthTotal
from app.services.categorization.catalog import CategoryCatalog

ZERO = Decimal("0.00")

# Meses cheios exibidos por padrão, contando o corrente.
DEFAULT_MONTHS = 12

# Os dois casos em que `category_id` não resolve para um nome. São textos
# diferentes de propósito: "sem categoria" é uma transação que ninguém classificou;
# "categoria inativa" aponta para uma categoria que o titular desativou — ela some
# do catálogo, mas não das transações já gravadas.
NO_CATEGORY = "Sem categoria"
INACTIVE_CATEGORY = "Categoria inativa"


def default_period(
    today: date_type, months: int = DEFAULT_MONTHS
) -> tuple[date_type, date_type]:
    """Primeiro dia do mês `months - 1` atrás, até hoje.

    Alinhado ao início do mês, e não `hoje - 365 dias`, para a série mensal não
    abrir com um balde pela metade.

    A aritmética passa por um índice de meses porque `date(ano - 1, mês, dia)`
    estoura em 29 de fevereiro — e um dashboard que quebra num dia por quadriênio é
    o tipo de defeito que só aparece em produção.
    """
    index = today.year * 12 + (today.month - 1) - (months - 1)
    return date_type(index // 12, index % 12 + 1, 1), today


def kind_total(rows: Sequence[Any], kind: TransactionKind) -> KindTotal:
    """Soma de um `kind`, com a fatia que ainda está sob revisão.

    As linhas vêm agrupadas por `(kind, categorization_status)`, então a fatia sai
    da mesma varredura do total — pedir as duas em consultas separadas abriria a
    porta para elas discordarem.
    """
    total, count, review_total, review_count = ZERO, 0, ZERO, 0
    for row_kind, status, amount, n in rows:
        if row_kind != kind.value:
            continue
        total += amount or ZERO
        count += n
        if status == CategorizationStatus.NEEDS_REVIEW.value:
            review_total += amount or ZERO
            review_count += n
    return KindTotal(
        total=total,
        count=count,
        needs_review_total=review_total,
        needs_review_count=review_count,
    )


def queue_counts(rows: Sequence[Any]) -> dict[str, int]:
    """Contagem por `categorization_status`, do mesmo recorte de período."""
    counts: dict[str, int] = {}
    for _kind, status, _amount, n in rows:
        counts[status] = counts.get(status, 0) + n
    return counts


def by_category(rows: Sequence[Any], catalog: CategoryCatalog) -> list[CategoryTotal]:
    """Quebra por categoria, com o rótulo resolvido pelo catálogo.

    O rótulo sai de `load_catalog` — o mesmo carregamento que monta o `enum` do JSON
    Schema do Ollama e o seletor da tela de revisão. Um join com CTE recursiva daria
    o mesmo nome hoje e divergiria no dia em que a regra de rótulo mudasse num lugar
    só.
    """
    totals = []
    for category_id, kind, amount, count, review_count in rows:
        if category_id is None:
            label = NO_CATEGORY
        else:
            entry = catalog.by_id.get(category_id)
            label = entry.label if entry is not None else INACTIVE_CATEGORY
        totals.append(
            CategoryTotal(
                category_id=category_id,
                label=label,
                kind=kind,
                total=amount or ZERO,
                count=count,
                needs_review_count=review_count,
            )
        )
    # Maior peso primeiro, por valor **absoluto**: despesa é negativa e receita
    # positiva, e ordenar pelo número cru jogaria o maior gasto para o fim da lista.
    totals.sort(key=lambda t: abs(t.total), reverse=True)
    return totals


def by_month(rows: Sequence[Any]) -> list[MonthTotal]:
    """Série mensal com os três `kind` em colunas.

    Mês sem lançamento nenhum não vira balde zerado: a tela desenha a partir do que
    existe, e fabricar os meses vazios aqui obrigaria esta função a conhecer o
    calendário do período. Mês que existe mas não tem um dos `kind` devolve zero,
    que é diferente de ausência.
    """
    months: dict[str, dict[str, Decimal]] = {}
    for month, kind, amount in rows:
        bucket = months.setdefault(month, {})
        bucket[kind] = bucket.get(kind, ZERO) + (amount or ZERO)

    return [
        MonthTotal(
            month=month,
            income=bucket.get(TransactionKind.INCOME.value, ZERO),
            expense=bucket.get(TransactionKind.EXPENSE.value, ZERO),
            transfer=bucket.get(TransactionKind.TRANSFER.value, ZERO),
        )
        for month, bucket in sorted(months.items())
    ]
