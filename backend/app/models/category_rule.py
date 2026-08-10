"""Regras de categorização — tabela vazia, ponto de extensão da pipeline híbrida.

Nada popula nem lê esta tabela hoje, conforme o escopo do MVP. O formato entra
agora, e não junto com a implementação, porque criar a tabela depois significaria
uma migration adicional num banco já com dados — enquanto criá-la vazia agora custa
nada e fixa o contrato para as camadas seguintes (regras → embeddings → LLM).
"""

import uuid
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    true,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, tenant_fk_column, uuid_pk
from app.models.enums import MatchType


class CategoryRule(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "category_rules"

    __table_args__ = (
        # Uma regra de texto precisa de padrão; uma de faixa de valor precisa de
        # pelo menos um limite. Sem isso, uma regra vazia casaria com tudo.
        CheckConstraint(
            "(match_type = 'AMOUNT_RANGE' AND (amount_min IS NOT NULL OR amount_max IS NOT NULL))"
            " OR (match_type <> 'AMOUNT_RANGE' AND pattern IS NOT NULL)",
            name="pattern_or_range",
        ),
        CheckConstraint(
            "amount_min IS NULL OR amount_max IS NULL OR amount_min <= amount_max",
            name="amount_range_ordered",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk_column()

    # Menor valor avalia primeiro. Empate resolve por `created_at`.
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default="100"
    )

    match_type: Mapped[str] = mapped_column(
        Enum(MatchType, native_enum=False, length=16, name="match_type"), nullable=False
    )
    pattern: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount_min: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    amount_max: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)

    category_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("categories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )


# Ordem de avaliação da pipeline de regras: só as ativas, já ordenadas.
Index(
    "ix_category_rules_active_priority",
    CategoryRule.tenant_id,
    CategoryRule.priority,
    postgresql_where=CategoryRule.is_active.is_(True),
)
