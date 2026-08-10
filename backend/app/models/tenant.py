"""Tenants e usuários.

Separados desde já, mesmo com um único usuário: `tenants` é a unidade de
isolamento de dados (e a raiz do CASCADE de eliminação), `users` é a unidade de
autenticação. Fundi-los agora custaria uma migração de dados quando o segundo
usuário do mesmo tenant aparecesse.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, func, true
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, uuid_pk


class Tenant(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(120), nullable=False)


class User(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# Unicidade sobre `lower(email)` e não sobre a coluna crua: sem isso `Pedro@x.com`
# e `pedro@x.com` viram duas contas para a mesma pessoa. Índice funcional em vez
# do tipo CITEXT porque não exige extensão no banco.
#
# Global, e não por tenant: o e-mail identifica o login, então precisa ser único
# em toda a instalação mesmo com o schema já multi-tenant.
Index("uq_users_email_lower", func.lower(User.email), unique=True)
