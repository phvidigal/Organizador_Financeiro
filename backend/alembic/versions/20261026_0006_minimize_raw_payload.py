"""Tira de `transactions.raw_payload` o dado pessoal de terceiros já gravado.

Até aqui `raw_payload` guardava a resposta da Pluggy inteira, e nela vem
`paymentData`: nome, CPF/CNPJ, agência e conta de quem mandou e de quem recebeu
cada Pix ou TED. Nada disso é do titular nem é necessário para reprocessar — e
ficava no banco sem criptografia, para sempre. `mappers.minimize_raw_payload`
passou a filtrar na gravação; esta migration aplica o mesmo filtro ao que já
estava lá.

**Cópia congelada da lista, e não `import` da função.** Migration descreve o que
foi feito num dia; se `RAW_TRANSACTION_FIELDS` crescer amanhã, rodar esta de novo
num banco novo não pode passar a fazer outra coisa. O teste em
`tests/test_migrations.py` confere que, hoje, as duas produzem o mesmo resultado.

**Irreversível.** O que sai não tem como voltar — é o objetivo. O `downgrade` não
faz nada, e isso não é esquecimento.

O dado antigo ainda ocupa páginas do Postgres até o próximo `VACUUM`; para que
saia do disco de fato, `VACUUM FULL transactions` (ver docs/pendencias-no-host.md).

Revision ID: 0006_minimize_raw_payload
Revises: 0005_categorization_reviews
Create Date: 2026-10-26
"""

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "0006_minimize_raw_payload"
down_revision: str | None = "0005_categorization_reviews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TRANSACTION_FIELDS = frozenset(
    {
        "id",
        "accountId",
        "description",
        "descriptionRaw",
        "amount",
        "amountInAccountCurrency",
        "currencyCode",
        "date",
        "type",
        "status",
        "category",
        "categoryId",
        "balance",
        "operationType",
        "providerCode",
        "providerId",
        "createdAt",
        "updatedAt",
        "merchant",
    }
)

_CREDIT_CARD_FIELDS = frozenset(
    {"installmentNumber", "totalInstallments", "totalAmount", "payeeMCC", "purchaseDate", "billId"}
)


def _document_type(party: Any) -> str | None:
    if not isinstance(party, dict):
        return None
    document = party.get("documentNumber")
    if not isinstance(document, dict):
        return None
    kind = document.get("type")
    return kind if isinstance(kind, str) else None


def _minimize(tx: dict[str, Any]) -> dict[str, Any]:
    raw = {key: value for key, value in tx.items() if key in _TRANSACTION_FIELDS}

    payment = tx.get("paymentData")
    if isinstance(payment, dict):
        kept: dict[str, Any] = {}
        if isinstance(payment.get("paymentMethod"), str):
            kept["paymentMethod"] = payment["paymentMethod"]
        for party in ("payer", "receiver"):
            document_type = _document_type(payment.get(party))
            if document_type is not None:
                kept[party] = {"documentNumber": {"type": document_type}}
        raw["paymentData"] = kept

    card = tx.get("creditCardMetadata")
    if isinstance(card, dict):
        raw["creditCardMetadata"] = {
            key: value for key, value in card.items() if key in _CREDIT_CARD_FIELDS
        }

    return raw


def upgrade() -> None:
    # Roda como owner, que ignora o RLS: é uma limpeza de todos os tenants.
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, raw_payload FROM transactions WHERE raw_payload IS NOT NULL")
    ).all()

    updates = []
    for row_id, payload in rows:
        if not isinstance(payload, dict):
            continue
        minimized = _minimize(payload)
        if minimized != payload:
            updates.append({"id": row_id, "payload": json.dumps(minimized)})

    if updates:
        bind.execute(
            sa.text("UPDATE transactions SET raw_payload = CAST(:payload AS jsonb) WHERE id = :id"),
            updates,
        )


def downgrade() -> None:
    # Irreversível por construção: o dado removido não existe mais em lugar nenhum.
    pass
