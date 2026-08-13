"""Schemas da categorização por LLM."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CategorizationOutcomeRead(BaseModel):
    """Resumo da última execução **deste processo**. Some no restart."""

    status: str
    started_at: datetime
    finished_at: datetime | None
    model: str | None
    processed: int
    categorized: int
    needs_review: int
    failed: int
    error: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)


class QueueCountsRead(BaseModel):
    """Contagem da fila, por `categorization_status`.

    É o número durável — ao contrário do `last_outcome`, sobrevive a um restart, e
    é o que a tela deve mostrar quando não há execução em andamento.
    """

    pending: int = 0
    categorized: int = 0
    needs_review: int = 0
    failed: int = 0


class CategorizationStatusRead(BaseModel):
    tenant_id: uuid.UUID
    running: bool
    queue: QueueCountsRead
    last_outcome: CategorizationOutcomeRead | None = None


class CategorizationResetRead(BaseModel):
    source: str
    transactions_reset: int
