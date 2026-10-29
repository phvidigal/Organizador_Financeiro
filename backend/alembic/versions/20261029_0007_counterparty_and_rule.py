"""Duas colunas em `transactions`: quem é a contraparte, e qual regra decidiu.

`counterparty_is_holder` responde "o outro lado deste Pix é o próprio titular?".
É a evidência que faltava para tirar uma transferência entre contas próprias dos
totais sem palpite: a Pluggy manda o CPF e o nome de quem pagou e de quem recebeu,
e o sync compara com o titular **em memória**. Só o resultado vem para cá — o
documento continua fora do banco, como desde a `0006`. NULL quando a origem não
mandou `paymentData`: "não sei" não é "não é".

`category_rule_id` diz qual regra do titular categorizou a linha. É o que permite
desativar uma regra e devolver à fila só o que ela decidiu, e responder "por que
essa categoria?" sem adivinhar. `SET NULL` e não `CASCADE`: apagar a regra não
apaga a transação.

Sem GRANT novo: o `app_user` já tem privilégio de tabela em `transactions`, e
coluna nova o herda.

Revision ID: 0007_counterparty_and_rule
Revises: 0006_minimize_raw_payload
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007_counterparty_and_rule"
down_revision: str | None = "0006_minimize_raw_payload"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "transactions", sa.Column("counterparty_is_holder", sa.Boolean(), nullable=True)
    )
    op.add_column(
        "transactions",
        sa.Column(
            "category_rule_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "category_rules.id",
                ondelete="SET NULL",
                name="fk_transactions_category_rule_id",
            ),
            nullable=True,
        ),
    )
    # Índice da FK: sem ele, desativar ou apagar uma regra faria seq scan em
    # `transactions` — o mesmo motivo de `ix_transactions_account_id`.
    op.create_index(
        "ix_transactions_category_rule_id", "transactions", ["category_rule_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_transactions_category_rule_id", table_name="transactions")
    op.drop_column("transactions", "category_rule_id")
    op.drop_column("transactions", "counterparty_is_holder")
