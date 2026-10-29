"""Schemas das regras do titular."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class RuleRead(BaseModel):
    id: uuid.UUID
    match_type: str
    # Como é comparado: caixa alta, sem acento (`app/core/text.py::fold`).
    pattern: str | None
    category_id: uuid.UUID
    # Rótulo qualificado, ou `None` se a categoria foi desativada depois — a regra
    # para de valer sem sumir da lista.
    category_label: str | None
    is_active: bool
    created_at: datetime
    # Quantas linhas esta regra decidiu e continuam com a decisão dela. É o número
    # que volta para a fila se ela for desativada.
    applied_count: int


class RuleUpdateRequest(BaseModel):
    is_active: bool


class RuleUpdatedRead(RuleRead):
    # Desativar devolve à fila o que a regra decidiu; reativar a aplica de novo.
    transactions_changed: int
