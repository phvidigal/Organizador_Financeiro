"""Transações — o núcleo do schema.

Esta tabela concentra as três decisões mais caras de reverter do projeto:
o tipo do valor monetário, a convenção de sinal e a estratégia de deduplicação.
"""

import uuid
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Date, DateTime

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, tenant_fk_column, uuid_pk
from app.models.enums import (
    CategorizationStatus,
    CategorySource,
    TransactionKind,
    TransactionSource,
)


class Transaction(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "transactions"

    __table_args__ = (
        # Toda linha precisa participar de um dos dois esquemas de unicidade
        # (external_id para Pluggy/OFX, dedup_hash para CSV/manual). Sem este
        # CHECK, um bug de ingestão gravaria linhas que nenhum índice protege e a
        # duplicação só apareceria no extrato do usuário.
        CheckConstraint(
            "external_id IS NOT NULL OR dedup_hash IS NOT NULL",
            name="dedup_key_present",
        ),
        CheckConstraint(
            "category_confidence IS NULL OR "
            "(category_confidence >= 0 AND category_confidence <= 1)",
            name="confidence_range",
        ),
        CheckConstraint("dedup_seq >= 0", name="dedup_seq_non_negative"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk_column()

    account_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )

    # --- Procedência ---------------------------------------------------------
    source: Mapped[str] = mapped_column(
        Enum(TransactionSource, native_enum=False, length=16, name="transaction_source"),
        nullable=False,
    )
    # UUID da Pluggy ou FITID do OFX. NULL em CSV e entrada manual.
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # --- Valor e data --------------------------------------------------------
    #
    # NUMERIC(18,2) e nunca REAL/float: ponto flutuante binário não representa
    # 0,10 exatamente, e o erro acumula em soma de extrato. Era o defeito do
    # protótipo em legacy/Banco_de_dados.py, e corrigir depois exigiria migrar
    # todos os dados já importados.
    #
    # Convenção de sinal normalizada na ingestão: negativo = saída, sempre.
    # A Pluggy usa `type` DEBIT/CREDIT e alguns connectors invertem a convenção
    # em cartão de crédito; deixar isso vazar para o banco significaria que todo
    # SUM() do dashboard precisaria conhecer as exceções. O payload original fica
    # em `raw_payload`, então nada se perde se a regra de normalização estiver errada.
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(
        String(3), nullable=False, default="BRL", server_default="BRL"
    )

    # Natureza da transação, ortogonal à categoria (ver TransactionKind).
    #
    # NOT NULL e **sem server_default**, ao contrário das outras colunas: um
    # default silencioso de 'EXPENSE' transformaria uma receita mal gravada em
    # gasto, e o erro só apareceria como um total errado no dashboard. Aqui é
    # melhor que a escrita falhe e alguém conserte o código que a esqueceu.
    kind: Mapped[str] = mapped_column(
        Enum(TransactionKind, native_enum=False, length=16, name="transaction_kind"),
        nullable=False,
    )

    date: Mapped[date_type] = mapped_column(Date, nullable=False)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- Descrição -----------------------------------------------------------
    # `description_raw` é imutável: é o insumo do LLM e da futura base de regras.
    # `description_clean` é derivada (sem ruído tipo "COMPRA CARTAO 1234").
    description_raw: Mapped[str] = mapped_column(Text, nullable=False)
    description_clean: Mapped[str | None] = mapped_column(Text, nullable=True)
    merchant: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # --- Categorização -------------------------------------------------------
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
    )

    # O que a Pluggy devolveu, preservado cru mesmo depois de mapeado para
    # `category_id`. É o que permite medir, na Fase 2, se a categorização nativa
    # basta — comparando-a com a correção manual do usuário.
    pluggy_category_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pluggy_category_name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Dois eixos ortogonais, não um campo só:
    #   categorization_status -> "já processei?"  (fila do job da Fase 3)
    #   category_source       -> "quem decidiu?"  (Pluggy, regra, LLM, humano)
    # Com um campo único seria impossível responder "quantas o LLM acertou?" sem
    # migração de dados depois.
    categorization_status: Mapped[str] = mapped_column(
        Enum(
            CategorizationStatus,
            native_enum=False,
            length=24,
            name="categorization_status",
        ),
        nullable=False,
        default=CategorizationStatus.PENDING,
        server_default=CategorizationStatus.PENDING.value,
    )
    category_source: Mapped[str | None] = mapped_column(
        Enum(CategorySource, native_enum=False, length=16, name="category_source"),
        nullable=True,
    )
    category_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    categorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- Deduplicação de origens sem identificador ---------------------------
    #
    # sha256 de (account_id | date | amount | descrição normalizada).
    dedup_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #
    # Ordinal da ocorrência do mesmo hash dentro do lote importado.
    #
    # É o que impede a constraint de rejeitar dado legítimo: dois cafés de R$ 12,00
    # no mesmo dia e no mesmo estabelecimento são duas transações reais, e um
    # UNIQUE(conta, data, valor, descrição) descartaria uma delas. Recebendo
    # dedup_seq 0 e 1, ambas entram. Reimportar o mesmo arquivo regenera os mesmos
    # pares (hash, seq), que colidem com o que já está gravado — nada duplica.
    dedup_seq: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")

    # Resposta crua da origem. Custa espaço e paga por si na primeira vez que a
    # normalização estiver errada e for preciso reprocessar sem novo sync.
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


# ---------------------------------------------------------------------------
# Índices
#
# Definidos fora da classe para poder usar expressões (DESC, filtros parciais)
# em vez de nomes de coluna em string.
# ---------------------------------------------------------------------------

# (a) Unicidade para origens com identificador estável — Pluggy (UUID) e OFX (FITID).
#
# Parcial (`WHERE external_id IS NOT NULL`) porque CSV e manual não têm o campo.
# Um índice único comum já toleraria os NULLs, já que NULLs não colidem entre si,
# mas o filtro explícito deixa a intenção legível e mantém o índice menor.
#
# `source` entra na chave porque o mesmo identificador pode, em tese, vir de duas
# origens diferentes (um FITID numérico do OFX colidindo com outro identificador).
Index(
    "uq_transactions_external",
    Transaction.tenant_id,
    Transaction.source,
    Transaction.external_id,
    unique=True,
    postgresql_where=Transaction.external_id.isnot(None),
)

# (b) Unicidade para origens sem identificador — CSV e entrada manual.
#
# `deleted_at IS NULL` no filtro: uma transação excluída logicamente não deve
# bloquear a reimportação da mesma linha depois.
Index(
    "uq_transactions_dedup",
    Transaction.tenant_id,
    Transaction.account_id,
    Transaction.dedup_hash,
    Transaction.dedup_seq,
    unique=True,
    postgresql_where=(Transaction.external_id.is_(None)) & (Transaction.deleted_at.is_(None)),
)

# Query principal do dashboard: extrato de uma conta, mais recente primeiro.
Index(
    "ix_transactions_tenant_account_date",
    Transaction.tenant_id,
    Transaction.account_id,
    Transaction.date.desc(),
)

# Timeline cross-account e agregações mensais.
Index("ix_transactions_tenant_date", Transaction.tenant_id, Transaction.date.desc())

# Filtro por categoria e somas por categoria. Também é o índice da FK: o Postgres
# não cria índice para chave estrangeira automaticamente, e sem ele todo UPDATE ou
# DELETE em `categories` faz seq scan em `transactions`.
Index("ix_transactions_tenant_category", Transaction.tenant_id, Transaction.category_id)

# Fila do job de categorização (Fase 3).
#
# Parcial de propósito: só as pendentes entram no índice, então ele encolhe à
# medida que o backlog é processado em vez de crescer junto com a tabela. Num
# índice completo sobre `categorization_status`, a consulta da fila teria de
# varrer milhares de linhas já categorizadas.
Index(
    "ix_transactions_pending",
    Transaction.tenant_id,
    Transaction.date,
    postgresql_where=(Transaction.categorization_status == CategorizationStatus.PENDING),
)

# Índice da FK de account_id, pelo mesmo motivo acima (CASCADE ao excluir a conta).
Index("ix_transactions_account_id", Transaction.account_id)
