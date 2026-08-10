"""Base declarativa, convenção de nomes e mixins compartilhados."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, MetaData, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import DateTime

# Precisa existir desde a PRIMEIRA migration. Sem convenção, o autogenerate cria
# constraints com nome gerado pelo Postgres, e nenhuma migration futura consegue
# referenciá-las para dropar ou alterar. Adicionar depois não renomeia o que já
# foi criado — por isso entra agora e não muda mais.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),  # pgcrypto/PG13+: nativo, sem extensão
    )


class TimestampMixin:
    """`created_at` / `updated_at` mantidos pelo banco.

    `updated_at` é atualizado por trigger (ver a migration inicial), não por
    `onupdate` do SQLAlchemy: o sync da Fase 2 grava via `INSERT ... ON CONFLICT`,
    que não passa pelo ORM e deixaria o timestamp congelado.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SoftDeleteMixin:
    """Exclusão lógica.

    Transação removida na origem não é apagada: perderíamos a categorização
    manual do usuário e a trilha de auditoria que a LGPD pede. A exclusão física
    fica reservada ao pedido de eliminação de dados do titular (Fase 5).
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )


def tenant_fk_column() -> Mapped[uuid.UUID]:
    """FK para `tenants`, sempre indexada.

    `ondelete="CASCADE"`: excluir o tenant leva junto todo o dado dele, que é
    exatamente o comportamento exigido pelo direito de eliminação (LGPD art. 18, VI).
    """
    return mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
