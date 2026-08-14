"""Schemas de transação."""

import uuid
from datetime import date as date_type
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import TransactionKind
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


class TransactionCategorizeRequest(BaseModel):
    """Correção manual de categoria, vinda da tela de revisão ou do extrato."""

    category_id: uuid.UUID = Field(
        description="id de uma categoria ativa do titular (ver GET /categories)"
    )

    # Opcional, e herda de `categories.kind` quando ausente — que é o comportamento
    # correto na esmagadora maioria dos casos (invariante 4).
    #
    # O override precisa existir porque a categoria não decide tudo: um Pix enviado
    # para pagar um serviço é despesa mesmo apontando para uma categoria TRANSFER, e
    # a origem do dado nunca sabe se o destino era conta do próprio titular. Só o
    # titular sabe, e esta é a tela em que ele responde.
    kind: TransactionKind | None = Field(
        default=None,
        description="sobrepõe o kind herdado da categoria; ausente = herda",
    )
