"""Categorias — padrão do sistema e customizadas pelo usuário."""

import uuid

from sqlalchemy import Boolean, Enum, ForeignKey, Index, String, false, true
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, tenant_fk_column, uuid_pk
from app.models.enums import TransactionKind


class Category(Base, TimestampMixin, SoftDeleteMixin):
    """Hierarquia de dois níveis, espelhando a taxonomia da Pluggy.

    As categorias padrão são **copiadas para cada tenant** no provisionamento, e
    não compartilhadas por linhas globais com `tenant_id IS NULL`. Duas razões:

    1. Linha global fura o modelo de RLS — a policy teria de abrir exceção para
       `tenant_id IS NULL`, e essa exceção é exatamente o tipo de brecha que o RLS
       existe para eliminar.
    2. O usuário precisa poder renomear, recolorir ou desativar uma categoria
       padrão sem afetar ninguém mais.

    O custo é ~50 linhas duplicadas por tenant, irrelevante nesta escala.
    """

    __tablename__ = "categories"

    __table_args__ = (
        # Uma categoria por nome dentro do mesmo pai. Vai como índice (mais abaixo)
        # e não como UniqueConstraint porque `parent_id` é NULL nas categorias raiz,
        # e NULLs não colidem numa constraint comum — duas raízes "Alimentação"
        # passariam batido.
        Index(
            "uq_categories_tenant_parent_name",
            "tenant_id",
            "parent_id",
            "name",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk_column()

    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("categories.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(80), nullable=False)  # exibido em pt-BR

    # Natureza declarada pela categoria: tudo sob "Transferências" é TRANSFER,
    # tudo sob "Receitas" é INCOME, o resto é EXPENSE.
    #
    # É daqui que a transação herda o `kind` ao ser categorizada. Sem isso, cada
    # camada da pipeline (regra, LLM, correção manual) teria de lembrar de ajustar
    # o `kind` junto — e a que esquecesse deixaria um Pix contando como gasto.
    kind: Mapped[str] = mapped_column(
        Enum(TransactionKind, native_enum=False, length=16, name="transaction_kind"),
        nullable=False,
        default=TransactionKind.EXPENSE,
        server_default=TransactionKind.EXPENSE.value,
    )

    # Categoria correspondente na taxonomia da Pluggy. É o que permite aproveitar
    # a categorização nativa (Fase 2) sem uma tabela extra de/para.
    pluggy_category_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    # Marca as categorias semeadas. Serve para restaurar os padrões e para não
    # tratar customização do usuário como bug de seed.
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )

    color: Mapped[str | None] = mapped_column(String(9), nullable=True)  # #RRGGBB
    icon: Mapped[str | None] = mapped_column(String(40), nullable=True)
