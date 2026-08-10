"""Schema inicial: tenants, contas, transações, categorias e RLS.

Escrita à mão em vez de gerada por --autogenerate porque três peças centrais não
têm representação no metadata do SQLAlchemy: as políticas de Row-Level Security,
a trigger de `updated_at` e o seed do tenant padrão.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-08-10
"""

import os
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --- Constantes compartilhadas ---------------------------------------------

# Mesmo UUID de app.core.tenancy.DEFAULT_TENANT_ID.
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# Namespace para gerar ids de categoria determinísticos (uuid5). Assim o mesmo
# banco recriado do zero produz os mesmos ids, o que torna os testes estáveis.
CATEGORY_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")

TENANT_SCOPED_TABLES = (
    "users",
    "bank_connections",
    "accounts",
    "categories",
    "transactions",
    "category_rules",
)

TIMESTAMPED_TABLES = ("tenants", *TENANT_SCOPED_TABLES)

APP_ROLE = os.getenv("APP_DB_USER", "app_user")


def _timestamps() -> list[sa.Column]:
    tz = sa.DateTime(timezone=True)
    return [
        sa.Column("created_at", tz, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", tz, server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", tz, nullable=True),
    ]


def _tenant_fk(table: str) -> sa.Column:
    """FK para `tenants`, com CASCADE.

    O CASCADE é o que faz o direito de eliminação da LGPD (art. 18, VI) ser um
    `DELETE FROM tenants` e não uma sequência manual de exclusões que alguém
    pode esquecer de manter atualizada quando uma tabela nova aparecer.

    O nome segue a convenção do `Base.metadata` (fk_<tabela>_<coluna>) para que o
    schema criado aqui e o descrito pelos modelos não divirjam.
    """
    return sa.Column(
        "tenant_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE", name=f"fk_{table}_tenant_id"),
        nullable=False,
    )


def upgrade() -> None:
    _create_tables()
    _create_indexes()
    _create_updated_at_trigger()
    _enable_row_level_security()
    _grant_app_role()
    _seed()


def downgrade() -> None:
    for table in reversed(TENANT_SCOPED_TABLES):
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
    for table in reversed(TIMESTAMPED_TABLES):
        op.execute(f'DROP TRIGGER IF EXISTS trg_{table}_updated_at ON "{table}"')
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")

    op.drop_table("category_rules")
    op.drop_table("transactions")
    op.drop_table("categories")
    op.drop_table("accounts")
    op.drop_table("bank_connections")
    op.drop_table("users")
    op.drop_table("tenants")


# ---------------------------------------------------------------------------
# Tabelas
# ---------------------------------------------------------------------------


def _create_tables() -> None:
    pg_uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB

    op.create_table(
        "tenants",
        # gen_random_uuid() é nativo do PostgreSQL desde a 13; não precisa de pgcrypto.
        sa.Column("id", pg_uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(120), nullable=False),
        *_timestamps(),
    )

    op.create_table(
        "users",
        sa.Column("id", pg_uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _tenant_fk("users"),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=True),
        sa.Column("full_name", sa.String(160), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )

    op.create_table(
        "bank_connections",
        sa.Column("id", pg_uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _tenant_fk("bank_connections"),
        sa.Column("pluggy_item_id", pg_uuid, nullable=False),
        sa.Column("connector_id", sa.Integer, nullable=True),
        sa.Column("connector_name", sa.String(120), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="UPDATING"),
        sa.Column("execution_status", sa.String(64), nullable=True),
        sa.Column("products", postgresql.ARRAY(sa.String(32)), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consent_granted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consent_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", jsonb, nullable=True),
        *_timestamps(),
        # Reconectar a mesma instituição não pode criar uma segunda conexão: o
        # sync passaria a rodar duas vezes sobre as mesmas contas.
        sa.UniqueConstraint(
            "tenant_id", "pluggy_item_id", name="uq_bank_connections_tenant_item"
        ),
        sa.CheckConstraint(
            "status IN ('UPDATING','UPDATED','WAITING_USER_INPUT',"
            "'LOGIN_ERROR','OUTDATED','ERROR')",
            name="connection_status",
        ),
    )

    op.create_table(
        "accounts",
        sa.Column("id", pg_uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _tenant_fk("accounts"),
        sa.Column(
            "bank_connection_id",
            pg_uuid,
            sa.ForeignKey(
                "bank_connections.id",
                ondelete="CASCADE",
                name="fk_accounts_bank_connection_id",
            ),
            nullable=True,
        ),
        sa.Column("pluggy_account_id", pg_uuid, nullable=True),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("subtype", sa.String(32), nullable=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("marketing_name", sa.String(160), nullable=True),
        sa.Column("number", sa.String(64), nullable=True),
        sa.Column("balance", sa.Numeric(18, 2), nullable=True),
        sa.Column("currency_code", sa.String(3), nullable=False, server_default="BRL"),
        sa.Column("bank_data", jsonb, nullable=True),
        sa.Column("credit_data", jsonb, nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "pluggy_account_id", name="uq_accounts_tenant_pluggy"),
        sa.CheckConstraint("type IN ('BANK','CREDIT')", name="account_type"),
        sa.CheckConstraint(
            "subtype IS NULL OR subtype IN ('CHECKING_ACCOUNT','SAVINGS_ACCOUNT','CREDIT_CARD')",
            name="account_subtype",
        ),
    )

    op.create_table(
        "categories",
        sa.Column("id", pg_uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _tenant_fk("categories"),
        sa.Column(
            "parent_id",
            pg_uuid,
            sa.ForeignKey("categories.id", ondelete="CASCADE", name="fk_categories_parent_id"),
            nullable=True,
        ),
        sa.Column("name", sa.String(80), nullable=False),
        # Natureza declarada pela categoria; a transação herda daqui ao ser
        # categorizada. Ver o comentário de TransactionKind em app/models/enums.py.
        sa.Column("kind", sa.String(16), nullable=False, server_default="EXPENSE"),
        sa.Column("pluggy_category_id", sa.String(32), nullable=True),
        sa.Column("is_system", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("color", sa.String(9), nullable=True),
        sa.Column("icon", sa.String(40), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "kind IN ('INCOME','EXPENSE','TRANSFER')", name="transaction_kind"
        ),
    )

    op.create_table(
        "transactions",
        sa.Column("id", pg_uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _tenant_fk("transactions"),
        sa.Column(
            "account_id",
            pg_uuid,
            sa.ForeignKey("accounts.id", ondelete="CASCADE", name="fk_transactions_account_id"),
            nullable=False,
        ),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=True),
        # NUMERIC, nunca float: 0,10 não tem representação exata em binário e o
        # erro acumula em soma de extrato. Era o defeito do protótipo SQLite.
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency_code", sa.String(3), nullable=False, server_default="BRL"),
        # NOT NULL e sem server_default, de propósito: um default silencioso de
        # 'EXPENSE' transformaria receita mal gravada em gasto, e o erro só
        # apareceria como total errado no dashboard. Melhor a escrita falhar.
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("description_raw", sa.Text, nullable=False),
        sa.Column("description_clean", sa.Text, nullable=True),
        sa.Column("merchant", jsonb, nullable=True),
        sa.Column(
            "category_id",
            pg_uuid,
            sa.ForeignKey("categories.id", ondelete="SET NULL", name="fk_transactions_category_id"),
            nullable=True,
        ),
        sa.Column("pluggy_category_id", sa.String(32), nullable=True),
        sa.Column("pluggy_category_name", sa.String(120), nullable=True),
        sa.Column(
            "categorization_status", sa.String(24), nullable=False, server_default="PENDING"
        ),
        sa.Column("category_source", sa.String(16), nullable=True),
        sa.Column("category_confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("categorized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dedup_hash", sa.String(64), nullable=True),
        sa.Column("dedup_seq", sa.Integer, nullable=False, server_default="0"),
        sa.Column("raw_payload", jsonb, nullable=True),
        *_timestamps(),
        # Toda linha precisa cair em um dos dois esquemas de unicidade. Sem isso,
        # um bug de ingestão gravaria linhas que nenhum índice protege e a
        # duplicação só apareceria no extrato do usuário.
        sa.CheckConstraint(
            "external_id IS NOT NULL OR dedup_hash IS NOT NULL",
            name="dedup_key_present",
        ),
        sa.CheckConstraint(
            "source IN ('PLUGGY','OFX','CSV','MANUAL')", name="transaction_source"
        ),
        sa.CheckConstraint(
            "kind IN ('INCOME','EXPENSE','TRANSFER')", name="transaction_kind"
        ),
        sa.CheckConstraint(
            "categorization_status IN ('PENDING','CATEGORIZED','NEEDS_REVIEW','FAILED')",
            name="categorization_status",
        ),
        sa.CheckConstraint(
            "category_source IS NULL OR "
            "category_source IN ('PLUGGY','RULE','EMBEDDING','LLM','MANUAL')",
            name="category_source",
        ),
        sa.CheckConstraint(
            "category_confidence IS NULL OR "
            "(category_confidence >= 0 AND category_confidence <= 1)",
            name="confidence_range",
        ),
        sa.CheckConstraint("dedup_seq >= 0", name="dedup_seq_non_negative"),
    )

    op.create_table(
        "category_rules",
        sa.Column("id", pg_uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _tenant_fk("category_rules"),
        sa.Column("priority", sa.Integer, nullable=False, server_default="100"),
        sa.Column("match_type", sa.String(16), nullable=False),
        sa.Column("pattern", sa.Text, nullable=True),
        sa.Column("amount_min", sa.Numeric(18, 2), nullable=True),
        sa.Column("amount_max", sa.Numeric(18, 2), nullable=True),
        sa.Column(
            "category_id",
            pg_uuid,
            sa.ForeignKey(
                "categories.id", ondelete="CASCADE", name="fk_category_rules_category_id"
            ),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.CheckConstraint(
            "match_type IN ('EXACT','CONTAINS','REGEX','AMOUNT_RANGE')",
            name="match_type",
        ),
        sa.CheckConstraint(
            "(match_type = 'AMOUNT_RANGE' AND (amount_min IS NOT NULL OR amount_max IS NOT NULL))"
            " OR (match_type <> 'AMOUNT_RANGE' AND pattern IS NOT NULL)",
            name="pattern_or_range",
        ),
        sa.CheckConstraint(
            "amount_min IS NULL OR amount_max IS NULL OR amount_min <= amount_max",
            name="amount_range_ordered",
        ),
    )


# ---------------------------------------------------------------------------
# Índices
# ---------------------------------------------------------------------------


def _create_indexes() -> None:
    # O PostgreSQL não cria índice para chave estrangeira automaticamente. Sem
    # eles, todo DELETE/UPDATE na tabela pai faz seq scan na tabela filha — e o
    # CASCADE de exclusão de tenant (LGPD) percorre justamente esses caminhos.
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])
    op.create_index("ix_bank_connections_tenant_id", "bank_connections", ["tenant_id"])
    op.create_index("ix_accounts_tenant_id", "accounts", ["tenant_id"])
    op.create_index("ix_accounts_bank_connection_id", "accounts", ["bank_connection_id"])
    op.create_index("ix_categories_tenant_id", "categories", ["tenant_id"])
    op.create_index("ix_categories_parent_id", "categories", ["parent_id"])
    op.create_index("ix_categories_pluggy_category_id", "categories", ["pluggy_category_id"])
    op.create_index("ix_transactions_tenant_id", "transactions", ["tenant_id"])
    op.create_index("ix_transactions_account_id", "transactions", ["account_id"])
    op.create_index("ix_category_rules_tenant_id", "category_rules", ["tenant_id"])
    op.create_index("ix_category_rules_category_id", "category_rules", ["category_id"])

    # E-mail único sem diferenciar maiúsculas: `Pedro@x.com` e `pedro@x.com` são
    # a mesma pessoa. Índice funcional em vez do tipo CITEXT porque não exige
    # extensão no banco.
    op.execute("CREATE UNIQUE INDEX uq_users_email_lower ON users (lower(email))")

    # NULLS NOT DISTINCT (PG 15+): sem isso, as categorias raiz — que têm
    # parent_id NULL — escapariam da unicidade, já que NULL nunca colide com NULL.
    op.execute(
        "CREATE UNIQUE INDEX uq_categories_tenant_parent_name "
        "ON categories (tenant_id, parent_id, name) NULLS NOT DISTINCT"
    )

    # ------------------------------------------------------------------
    # Deduplicação de transações — o ponto central deste schema.
    #
    # São duas constraints e não uma porque a natureza do identificador muda com
    # a origem do dado.
    # ------------------------------------------------------------------

    # (a) Origens com identificador estável: Pluggy (UUID) e OFX (FITID).
    #
    # Parcial porque CSV e entrada manual não têm o campo. Um índice único comum
    # já toleraria os NULLs (NULLs não colidem entre si), mas o filtro explícito
    # documenta a intenção e mantém o índice menor.
    op.execute(
        "CREATE UNIQUE INDEX uq_transactions_external "
        "ON transactions (tenant_id, source, external_id) "
        "WHERE external_id IS NOT NULL"
    )

    # (b) Origens sem identificador: CSV e entrada manual.
    #
    # O ingênuo seria UNIQUE(conta, data, valor, descrição) — que REJEITA DADO
    # LEGÍTIMO: dois cafés de R$ 12,00 no mesmo dia e no mesmo lugar são duas
    # transações reais, e o usuário perderia uma delas.
    #
    # Com (dedup_hash, dedup_seq) as duas entram, recebendo seq 0 e 1. E
    # reimportar o mesmo arquivo regenera exatamente os mesmos pares, que colidem
    # com o que já está gravado — nada duplica. Determinístico, sem heurística de
    # "parece duplicado".
    #
    # `deleted_at IS NULL` no filtro: uma transação excluída logicamente não deve
    # impedir a reimportação da mesma linha mais tarde.
    op.execute(
        "CREATE UNIQUE INDEX uq_transactions_dedup "
        "ON transactions (tenant_id, account_id, dedup_hash, dedup_seq) "
        "WHERE external_id IS NULL AND deleted_at IS NULL"
    )

    # Query principal do dashboard: extrato de uma conta, mais recente primeiro.
    op.execute(
        "CREATE INDEX ix_transactions_tenant_account_date "
        "ON transactions (tenant_id, account_id, date DESC)"
    )
    # Timeline cross-account e agregações mensais.
    op.execute("CREATE INDEX ix_transactions_tenant_date ON transactions (tenant_id, date DESC)")
    # Filtro e somatório por categoria.
    op.execute(
        "CREATE INDEX ix_transactions_tenant_category ON transactions (tenant_id, category_id)"
    )

    # Fila do job de categorização (Fase 3). Parcial de propósito: só as pendentes
    # entram, então o índice encolhe conforme o backlog é processado em vez de
    # crescer junto com a tabela.
    op.execute(
        "CREATE INDEX ix_transactions_pending ON transactions (tenant_id, date) "
        "WHERE categorization_status = 'PENDING'"
    )

    # Ordem de avaliação da futura pipeline de regras.
    op.execute(
        "CREATE INDEX ix_category_rules_active_priority "
        "ON category_rules (tenant_id, priority) WHERE is_active"
    )


# ---------------------------------------------------------------------------
# updated_at por trigger
# ---------------------------------------------------------------------------


def _create_updated_at_trigger() -> None:
    """Mantém `updated_at` correto em qualquer caminho de escrita.

    Trigger em vez do `onupdate` do SQLAlchemy porque o sync da Fase 2 grava via
    `INSERT ... ON CONFLICT DO UPDATE`, que não passa pelo ORM — o timestamp
    ficaria congelado justamente nas linhas atualizadas por re-sincronização.
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table in TIMESTAMPED_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_updated_at
            BEFORE UPDATE ON "{table}"
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            """
        )


# ---------------------------------------------------------------------------
# Row-Level Security
# ---------------------------------------------------------------------------


def _enable_row_level_security() -> None:
    """Isolamento por tenant imposto pelo banco, não pela aplicação.

    Um único `WHERE tenant_id = ...` esquecido numa query vaza dado entre tenants
    — sob a LGPD isso é incidente de segurança, não bug de listagem. Com a policy,
    a query esquecida simplesmente não retorna nada.

    `NULLIF(..., '')` antes do cast: `current_setting(..., true)` devolve NULL
    quando a variável não foi definida (e aí a comparação filtra tudo, que é o
    comportamento desejado), mas devolveria string vazia se alguém a definisse
    como '' — e `''::uuid` levanta erro em vez de filtrar.
    """
    predicate = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"

    for table in TENANT_SCOPED_TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        # USING filtra leitura; WITH CHECK impede gravar linha de outro tenant.
        # Só com os dois, um INSERT com tenant_id forjado é bloqueado.
        op.execute(
            f'CREATE POLICY tenant_isolation ON "{table}" '
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )


def _grant_app_role() -> None:
    """Privilégios do role da aplicação.

    Grant tabela a tabela, e não `ON ALL TABLES`, para não incluir
    `alembic_version` — a aplicação não tem por que escrever no controle de
    versão do schema.

    O bloco condicional existe porque o role é criado pelo entrypoint do container
    do Postgres. Num banco criado por outro caminho (a base de testes, por
    exemplo) ele pode não existir, e a migration não deve quebrar por isso.
    """
    tables = ", ".join(f'"{t}"' for t in TIMESTAMPED_TABLES)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                GRANT USAGE ON SCHEMA public TO "{APP_ROLE}";
                GRANT SELECT, INSERT, UPDATE, DELETE ON {tables} TO "{APP_ROLE}";
            END IF;
        END
        $$;
        """
    )


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------

# Taxonomia inicial em pt-BR. `pluggy_category_id` fica NULL de propósito: o
# de/para com a taxonomia da Pluggy é preenchido na Fase 2, a partir de
# `GET /categories`. Chutar os ids agora criaria mapeamentos errados que passariam
# despercebidos até alguém conferir uma categorização no dashboard.
#
# Cada raiz declara seu `kind`, herdado pelas subcategorias. É o que faz o
# dashboard somar só EXPENSE sem precisar conhecer nomes de categoria.
#
# "Transferências" é TRANSFER e categoria de primeira classe porque, no Brasil,
# Pix e pagamento de fatura dominam o extrato sem serem gasto: um Pix de R$ 2.000
# entre contas do próprio titular apareceria como R$ 2.000 de despesa numa conta e
# R$ 2.000 de receita na outra.
#
# É também o que torna seguro contar cada compra do cartão como despesa na data da
# compra: "Pagamento de cartão" entra como TRANSFER e não soma de novo.
DEFAULT_CATEGORIES: dict[str, tuple[str, list[str]]] = {
    "Receitas": (
        "INCOME",
        [
            "Salário",
            "Freelance / PJ",
            "Rendimentos e investimentos",
            "Reembolsos",
            "Outras receitas",
        ],
    ),
    "Moradia": (
        "EXPENSE",
        [
            "Aluguel / Financiamento",
            "Condomínio",
            "Água, luz e gás",
            "Internet e telefone",
            "Manutenção e reformas",
        ],
    ),
    "Alimentação": ("EXPENSE", ["Supermercado", "Restaurantes e bares", "Delivery"]),
    "Transporte": (
        "EXPENSE",
        [
            "Combustível",
            "Transporte por aplicativo",
            "Transporte público",
            "Estacionamento e pedágio",
            "Manutenção do veículo",
        ],
    ),
    "Saúde": (
        "EXPENSE",
        ["Plano de saúde", "Farmácia", "Consultas e exames", "Academia e bem-estar"],
    ),
    "Educação": ("EXPENSE", ["Cursos e mensalidades", "Livros e materiais"]),
    "Lazer": ("EXPENSE", ["Streaming e assinaturas", "Viagens", "Eventos e cultura"]),
    "Compras": ("EXPENSE", ["Vestuário", "Eletrônicos", "Casa e decoração"]),
    "Serviços financeiros": (
        "EXPENSE",
        ["Tarifas bancárias", "Juros e encargos", "Impostos", "Seguros"],
    ),
    "Transferências": (
        "TRANSFER",
        [
            "Pix enviado",
            "Pix recebido",
            "Transferência entre contas próprias",
            "Pagamento de cartão",
        ],
    ),
    "Outros": ("EXPENSE", ["Não categorizado"]),
}


def category_uuid(tenant_id: uuid.UUID, parent: str | None, name: str) -> uuid.UUID:
    """Id determinístico por (tenant, pai, nome).

    uuid5 em vez de uuid4 para que recriar o banco do zero produza os mesmos ids
    — o que mantém os testes estáveis e torna dumps comparáveis entre ambientes.
    """
    return uuid.uuid5(CATEGORY_NAMESPACE, f"{tenant_id}:{parent or ''}:{name}")


def _seed() -> None:
    """Tenant único do MVP e suas categorias padrão.

    As categorias são copiadas por tenant, e não compartilhadas por linhas globais
    com `tenant_id IS NULL`. Linha global obrigaria a policy de RLS a abrir uma
    exceção — exatamente o tipo de brecha que o RLS existe para eliminar — e
    impediria o usuário de renomear ou desativar uma categoria padrão.
    """
    conn = op.get_bind()

    conn.execute(
        sa.text("INSERT INTO tenants (id, name) VALUES (:id, :name)"),
        {"id": str(DEFAULT_TENANT_ID), "name": "Pessoal"},
    )

    rows = []
    for parent_name, (kind, children) in DEFAULT_CATEGORIES.items():
        parent_id = category_uuid(DEFAULT_TENANT_ID, None, parent_name)
        rows.append(
            {"id": str(parent_id), "parent_id": None, "name": parent_name, "kind": kind}
        )
        for child_name in children:
            # Subcategoria herda o kind da raiz: não faz sentido "Pix enviado"
            # ser TRANSFER e "Pix recebido" ser outra coisa.
            rows.append(
                {
                    "id": str(category_uuid(DEFAULT_TENANT_ID, parent_name, child_name)),
                    "parent_id": str(parent_id),
                    "name": child_name,
                    "kind": kind,
                }
            )

    conn.execute(
        sa.text(
            "INSERT INTO categories (id, tenant_id, parent_id, name, kind, is_system) "
            "VALUES (:id, :tenant_id, :parent_id, :name, :kind, true)"
        ),
        [{**row, "tenant_id": str(DEFAULT_TENANT_ID)} for row in rows],
    )
