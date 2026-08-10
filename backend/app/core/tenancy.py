"""Escopo de tenant — o único lugar que ativa o Row-Level Security.

Todo acesso a dado de tenant passa por aqui. As policies criadas na migration
inicial filtram por `current_setting('app.tenant_id')`, então uma sessão que não
passe por esta dependency simplesmente não enxerga linha nenhuma — o modo de
falha é "não vê nada", nunca "vê o tenant errado".
"""

import uuid
from collections.abc import AsyncIterator

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import SessionLocal

# Tenant único do MVP, criado pela migration inicial. Vira o `sub` de um JWT
# quando a autenticação entrar (Fase 2).
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def resolve_tenant_id(
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> uuid.UUID:
    """Descobre o tenant do request.

    Enquanto não há autenticação, aceita o header `X-Tenant-Id` e cai no tenant
    padrão. Isso é conveniente para desenvolvimento e inaceitável em produção —
    daí o bloqueio explícito abaixo, para que a lacuna não passe despercebida
    num deploy.
    """
    settings = get_settings()

    if x_tenant_id is not None:
        try:
            return uuid.UUID(x_tenant_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X-Tenant-Id não é um UUID válido",
            ) from None

    if settings.environment == "production":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autenticação obrigatória: nenhum tenant no request",
        )

    return DEFAULT_TENANT_ID


async def set_tenant_scope(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Aplica o tenant à transação corrente.

    `set_config(..., is_local => true)` equivale a `SET LOCAL`: o valor é
    descartado no fim da transação. Isso importa com pool de conexões — sem o
    escopo local, o tenant vazaria para o próximo request que reaproveitasse a
    mesma conexão física.

    Vai como parâmetro de bind, e não interpolado na string, porque `SET` não
    aceita placeholder e concatenar UUID em SQL abriria injeção.
    """
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


async def get_tenant_session(
    tenant_id: uuid.UUID = Depends(resolve_tenant_id),
) -> AsyncIterator[AsyncSession]:
    """Sessão com RLS ativo, dentro de uma transação explícita.

    A transação é aberta aqui de propósito: `SET LOCAL` fora de transação é
    silenciosamente ignorado pelo Postgres, e o resultado seria uma sessão sem
    escopo nenhum sem qualquer erro visível.
    """
    async with SessionLocal() as session, session.begin():
        await set_tenant_scope(session, tenant_id)
        yield session
