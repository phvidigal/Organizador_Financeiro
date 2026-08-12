"""Schema de transação."""

import uuid
from datetime import date as date_type
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.common import Money


class TransactionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    source: str
    external_id: str | None

    amount: Money
    currency_code: str
    # INCOME / EXPENSE / TRANSFER. O dashboard soma por aqui e agrupa por
    # categoria — os dois eixos são separados de propósito.
    kind: str

    date: date_type
    # NULL enquanto a transação não compensou.
    posted_at: datetime | None

    description_raw: str
    description_clean: str | None

    category_id: uuid.UUID | None
    categorization_status: str
    category_source: str | None
    category_confidence: Money | None

    # O palpite da Pluggy, guardado mas não adotado. É a régua com que a Fase 3
    # mede se a categorização nativa bastaria.
    pluggy_category_id: str | None
    pluggy_category_name: str | None

    # `raw_payload` e `merchant` ficam de fora: o primeiro é insumo de
    # reprocessamento, não de tela, e ambos carregam dado da instituição que não
    # precisa atravessar a rede a cada listagem.
