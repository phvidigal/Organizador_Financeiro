"""Schemas do dashboard.

Espelham os dataclasses de `app/services/dashboard.py`. Todo valor é `Money` —
string com duas casas — pelo motivo de sempre: o total é exatamente o número que
não pode virar double no `JSON.parse` do navegador.
"""

import uuid
from datetime import date as date_type

from pydantic import BaseModel, ConfigDict

from app.schemas.common import Money


class KindTotalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # Com sinal: despesa é negativa, e um estorno categorizado como despesa a abate.
    total: Money
    inflow: Money
    outflow: Money
    count: int
    # A parte de `total` que ainda espera decisão. Está dentro do total, não fora.
    undecided_total: Money
    undecided_count: int


class CategoryTotalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # NULL no balde "Sem categoria".
    category_id: uuid.UUID | None
    label: str
    name: str
    total: Money
    count: int
    undecided_count: int


class RootTotalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # NULL nos baldes "Sem categoria" e "Categoria inativa", que não são raiz de
    # árvore nenhuma. O rótulo os distingue.
    root_id: uuid.UUID | None
    label: str
    total: Money
    count: int
    undecided_count: int
    children: list[CategoryTotalRead]


class UndecidedCountsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    pending: int
    needs_review: int
    failed: int


class DashboardSummaryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    date_from: date_type
    date_to: date_type
    account_id: uuid.UUID | None
    currency_code: str

    income: KindTotalRead
    expense: KindTotalRead
    # Fora de receita e despesa — é a razão de `kind` existir. Vai junto para a tela
    # mostrar quanto dinheiro só mudou de lugar.
    transfer: KindTotalRead
    net: Money

    income_by_category: list[RootTotalRead]
    expense_by_category: list[RootTotalRead]
    transfer_by_category: list[RootTotalRead]

    undecided: UndecidedCountsRead
    # Linhas do período em moeda diferente de `currency_code`, fora dos totais.
    other_currency_count: int
    # Lançamento mais recente, ignorando o período. Ver `DashboardSummary`.
    latest_date: date_type | None
