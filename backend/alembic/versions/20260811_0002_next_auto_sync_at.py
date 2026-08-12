"""Guarda quando a Pluggy vai sincronizar o item sozinha.

Descoberto na primeira chamada real da Fase 2: no tier pessoal, `PATCH /items/{id}`
responde `400 "MeuPluggy item cant be updated"`. Não dá para pedir atualização — a
Pluggy sincroniza por conta própria a cada ~24h e anuncia a próxima em
`nextAutoSyncAt`.

Isso muda o que a interface pode dizer. Sem esta coluna, a tela só consegue mostrar
"sincronizado às HH:MM", e o usuário fica clicando em sincronizar sem entender por
que nada muda. Com ela, mostra "a Pluggy atualiza de novo às HH:MM", que é a única
informação que responde a pergunta de verdade.

Revision ID: 0002_next_auto_sync_at
Revises: 0001_initial_schema
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_next_auto_sync_at"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable e sem default: nem todo connector promete próxima sincronização, e
    # inventar um horário seria pior que admitir que não se sabe.
    op.add_column(
        "bank_connections",
        sa.Column("next_auto_sync_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bank_connections", "next_auto_sync_at")
