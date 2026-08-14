"""Tabela `categorization_reviews` — o histórico que a correção manual apagaria.

A Fase 4 dá ao titular o botão para corrigir a categoria. O efeito colateral é que
gravar a correção **sobrescreve** `category_id` e `category_confidence`, e com eles
some o par que a Fase 3 registrou como pendente: *"se `0.450` erra mais que
`0.950`, só as correções `MANUAL` dirão"*. Depois do UPDATE não dá nem para saber
se o titular confirmou a escolha do LLM ou a corrigiu — que é precisamente a
distinção a medir.

Esta tabela grava o estado anterior antes de cada correção. O racional completo
está no cabeçalho de `app/models/categorization_review.py`.

Escrita à mão, como as anteriores: a policy de RLS e o GRANT não têm representação
no metadata do SQLAlchemy e não sairiam do `--autogenerate`.

Revision ID: 0005_categorization_reviews
Revises: 0004_pix_recebido_receita
Create Date: 2026-08-14
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_categorization_reviews"
down_revision: str | None = "0004_pix_recebido_receita"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "categorization_reviews"

APP_ROLE = os.getenv("APP_DB_USER", "app_user")

# O mesmo predicado da migration inicial. `NULLIF(..., '')` antes do cast porque
# `current_setting(..., true)` devolve NULL quando a variável não existe (e aí a
# comparação filtra tudo, que é o desejado), mas devolveria string vazia se alguém
# a definisse como '' — e `''::uuid` levanta erro em vez de filtrar.
RLS_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    pg_uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        TABLE,
        sa.Column(
            "id",
            pg_uuid,
            primary_key=True,
            server_default=sa.func.gen_random_uuid(),
        ),
        sa.Column(
            "tenant_id",
            pg_uuid,
            sa.ForeignKey("tenants.id", ondelete="CASCADE", name=f"fk_{TABLE}_tenant_id"),
            nullable=False,
        ),
        sa.Column(
            "transaction_id",
            pg_uuid,
            sa.ForeignKey(
                "transactions.id", ondelete="CASCADE", name=f"fk_{TABLE}_transaction_id"
            ),
            nullable=False,
        ),
        # --- Estado anterior. Tudo nulo é legítimo: é a transação que nunca passou
        # pelo LLM e foi categorizada direto pelo titular no extrato.
        sa.Column(
            "previous_category_id",
            pg_uuid,
            sa.ForeignKey(
                "categories.id", ondelete="SET NULL", name=f"fk_{TABLE}_previous_category_id"
            ),
            nullable=True,
        ),
        sa.Column("previous_kind", sa.String(16), nullable=True),
        sa.Column("previous_source", sa.String(16), nullable=True),
        sa.Column("previous_status", sa.String(24), nullable=True),
        sa.Column("previous_confidence", sa.Numeric(4, 3), nullable=True),
        # --- Resposta do titular. `new_category_id` é sempre preenchida na escrita,
        # mas anulável na coluna para o `SET NULL` da FK ter espaço para agir.
        sa.Column(
            "new_category_id",
            pg_uuid,
            sa.ForeignKey(
                "categories.id", ondelete="SET NULL", name=f"fk_{TABLE}_new_category_id"
            ),
            nullable=True,
        ),
        sa.Column("new_kind", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Os CHECKs que substituem o ENUM nativo, como no resto do schema.
        sa.CheckConstraint(
            "previous_kind IS NULL OR previous_kind IN ('INCOME','EXPENSE','TRANSFER')",
            name="previous_kind",
        ),
        sa.CheckConstraint(
            "previous_source IS NULL OR "
            "previous_source IN ('PLUGGY','RULE','EMBEDDING','LLM','MANUAL')",
            name="previous_source",
        ),
        sa.CheckConstraint(
            "previous_status IS NULL OR "
            "previous_status IN ('PENDING','CATEGORIZED','NEEDS_REVIEW','FAILED')",
            name="previous_status",
        ),
        sa.CheckConstraint(
            "previous_confidence IS NULL OR "
            "(previous_confidence >= 0 AND previous_confidence <= 1)",
            name="previous_confidence_range",
        ),
        sa.CheckConstraint("new_kind IN ('INCOME','EXPENSE','TRANSFER')", name="new_kind"),
    )

    op.create_index(f"ix_{TABLE}_tenant_id", TABLE, ["tenant_id"])
    # Índice da FK de `transaction_id`: é de `transactions` que o CASCADE parte.
    op.create_index(f"ix_{TABLE}_transaction_id", TABLE, ["transaction_id", "tenant_id"])
    # A leitura da medição de calibração.
    op.execute(
        f"CREATE INDEX ix_{TABLE}_tenant_created ON {TABLE} (tenant_id, created_at DESC)"
    )

    _enable_row_level_security()
    _grant_app_role()


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {TABLE}")
    op.drop_table(TABLE)


def _enable_row_level_security() -> None:
    """Isolamento imposto pelo banco, como em toda tabela com `tenant_id`.

    Sem isto a tabela nova escaparia do modelo — e o `WITH CHECK` importa tanto
    quanto o `USING`: só com os dois um INSERT com `tenant_id` forjado é bloqueado.
    """
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {TABLE} "
        f"USING ({RLS_PREDICATE}) WITH CHECK ({RLS_PREDICATE})"
    )


def _grant_app_role() -> None:
    """Privilégios do role da aplicação — e a ausência deliberada de `UPDATE`.

    A migration inicial concede privilégios iterando a lista de tabelas que existia
    naquele momento. Tabela criada depois não herda nada: sem este bloco, o
    `app_user` toma `permission denied for table categorization_reviews` no primeiro
    INSERT — e como isso só acontece na primeira correção manual, o erro apareceria
    na tela do titular, não em teste.

    **Sem `UPDATE`, de propósito.** É o que torna o log append-only por privilégio
    e não por convenção: um caminho de código que tente reescrever uma revisão falha
    no banco. `DELETE` fica porque a eliminação do titular (LGPD art. 18, VI) é um
    `DELETE FROM tenants` que cascateia até aqui.

    O bloco condicional existe porque o role é criado pelo entrypoint do container
    do Postgres; num banco criado por outro caminho (a base de testes) ele pode não
    existir, e a migration não deve quebrar por isso.
    """
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                GRANT SELECT, INSERT, DELETE ON {TABLE} TO "{APP_ROLE}";
            END IF;
        END
        $$;
        """
    )
