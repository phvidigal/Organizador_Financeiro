"""Contas bancárias e cartões sincronizados."""

import uuid
from decimal import Decimal

from sqlalchemy import Enum, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, tenant_fk_column, uuid_pk
from app.models.enums import AccountSubtype, AccountType


class Account(Base, TimestampMixin, SoftDeleteMixin):
    """Conta sincronizada via Pluggy, ou criada manualmente para importar OFX/CSV.

    Campos deliberadamente ausentes: `taxNumber` (CPF) e `owner` (nome do titular),
    que a Pluggy devolve mas o MVP não usa. Minimização de dados é princípio da
    LGPD (art. 6º, III) — dado pessoal que não é necessário não deve ser coletado.
    Se virarem necessários, entram criptografados junto com a Fase 5.
    """

    __tablename__ = "accounts"

    __table_args__ = (
        UniqueConstraint("tenant_id", "pluggy_account_id", name="uq_accounts_tenant_pluggy"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk_column()

    bank_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("bank_connections.id", ondelete="CASCADE"),
        nullable=True,  # NULL = conta manual, sem vínculo com a Pluggy
        index=True,  # o Postgres não indexa FK sozinho; sem isso o CASCADE faz seq scan
    )

    # NULL para contas manuais — daí a unicidade acima tolerar NULLs (não colidem).
    pluggy_account_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )

    type: Mapped[str] = mapped_column(
        Enum(AccountType, native_enum=False, length=16, name="account_type"), nullable=False
    )
    subtype: Mapped[str | None] = mapped_column(
        Enum(AccountSubtype, native_enum=False, length=32, name="account_subtype"), nullable=True
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    marketing_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    number: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # NUMERIC, nunca float: ver a nota em Transaction.amount.
    balance: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    currency_code: Mapped[str] = mapped_column(
        String(3), nullable=False, default="BRL", server_default="BRL"
    )

    # Blocos `bankData` / `creditData` da Pluggy guardados crus: os campos variam
    # por connector e não vale modelar coluna a coluna antes de saber quais importam.
    bank_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    credit_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
