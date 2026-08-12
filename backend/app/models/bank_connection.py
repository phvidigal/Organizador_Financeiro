"""Conexão bancária — um Item da Pluggy."""

import uuid
from datetime import datetime

from sqlalchemy import Enum, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, tenant_fk_column, uuid_pk
from app.models.enums import ConnectionStatus


class BankConnection(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "bank_connections"

    __table_args__ = (
        # Reconectar a mesma instituição não pode gerar uma segunda conexão: o
        # sync passaria a rodar duas vezes sobre as mesmas contas.
        UniqueConstraint("tenant_id", "pluggy_item_id", name="uq_bank_connections_tenant_item"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk_column()

    pluggy_item_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    connector_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    connector_name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    status: Mapped[str] = mapped_column(
        Enum(ConnectionStatus, native_enum=False, length=32, name="connection_status"),
        nullable=False,
        default=ConnectionStatus.UPDATING,
        server_default=ConnectionStatus.UPDATING.value,
    )
    # String livre e não Enum: a Pluggy documenta os valores de executionStatus de
    # forma menos estável que os de status, e um CHECK apertado transformaria um
    # valor novo da API em erro de escrita no meio de um sync.
    execution_status: Mapped[str | None] = mapped_column(String(64), nullable=True)

    products: Mapped[list[str] | None] = mapped_column(ARRAY(String(32)), nullable=True)

    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Quando a Pluggy vai buscar dado novo na instituição por conta própria.
    #
    # No tier pessoal não dá para pedir atualização (`PATCH /items` responde 400
    # para item do connector MeuPluggy), então este campo é a única resposta
    # honesta para "quando vou ver lançamento novo?". Sem ele a tela mostraria só
    # a hora da última leitura, e o usuário ficaria sincronizando à toa.
    next_auto_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Consentimento (LGPD / Open Finance) ---------------------------------
    # Já aqui, não só na Fase 5: o consentimento do Open Finance expira (~12 meses)
    # e o sync precisa saber disso para avisar o usuário antes de a conexão morrer.
    consent_granted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consent_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Último erro reportado pela Pluggy (code/message/providerMessage), cru.
    error: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
