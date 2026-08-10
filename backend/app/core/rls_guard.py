"""Verificação de boot: o RLS realmente se aplica a esta conexão?

O isolamento entre tenants depende de um detalhe silencioso — o role que a
aplicação usa **não** pode ser dono das tabelas, nem superusuário, nem ter
BYPASSRLS. Se qualquer um desses for verdade, o PostgreSQL ignora as policies e
todas as queries passam a enxergar todos os tenants. Sem erro, sem log, sem
sintoma: o app funciona perfeitamente e vaza tudo.

E é uma linha de `.env` de distância. Trocar `DATABASE_URL` de `app_user` para
`postgres` durante uma depuração e esquecer de voltar é o cenário realista.

Por isso a verificação roda no startup e derruba o processo. Um container que não
sobe é um problema de dez minutos; um isolamento desligado em silêncio pode levar
meses para ser notado — e sob a LGPD é incidente de segurança, não bug.
"""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import TENANT_SCOPED_TABLES

logger = logging.getLogger(__name__)


class RLSNotEnforcedError(RuntimeError):
    """A conexão da aplicação escapa das políticas de isolamento."""


async def assert_rls_enforced(engine: AsyncEngine) -> None:
    """Falha se o role da conexão puder ignorar Row-Level Security.

    Levanta `RLSNotEnforcedError` — não apenas loga. Um aviso em log seria
    ignorado exatamente na situação em que importa.
    """
    async with engine.connect() as conn:
        role = await conn.scalar(text("SELECT current_user"))

        privileges = (
            await conn.execute(
                text(
                    "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()

        problems = []
        if privileges.rolsuper:
            problems.append("é SUPERUSER")
        if privileges.rolbypassrls:
            problems.append("tem BYPASSRLS")

        # O dono da tabela também ignora as policies, a menos que a tabela use
        # FORCE ROW LEVEL SECURITY. Este projeto não usa FORCE de propósito: é o
        # que permite as migrations rodarem com o owner.
        owned = (
            await conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' "
                    "AND tablename = ANY(:tables) AND tableowner = current_user"
                ),
                {"tables": list(TENANT_SCOPED_TABLES)},
            )
        ).scalars().all()

        if owned:
            problems.append(f"é dono das tabelas ({', '.join(owned)})")

        if problems:
            raise RLSNotEnforcedError(
                f"O role '{role}' ignora Row-Level Security: {'; '.join(problems)}. "
                f"A aplicação deve conectar com um role sem privilégio de dono "
                f"(APP_DB_USER no .env). Conectar como owner desliga o isolamento "
                f"entre tenants sem produzir nenhum erro visível."
            )

        # Tabela ausente não é erro: o container sobe antes de `alembic upgrade
        # head` na primeira vez. O que importa é que nenhuma das que existem
        # esteja desprotegida.
        unprotected = (
            await conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                    "AND c.relname = ANY(:tables) AND NOT c.relrowsecurity"
                ),
                {"tables": list(TENANT_SCOPED_TABLES)},
            )
        ).scalars().all()

        if unprotected:
            raise RLSNotEnforcedError(
                f"Tabelas com tenant_id e sem Row-Level Security ativo: "
                f"{', '.join(unprotected)}. Falta ENABLE ROW LEVEL SECURITY e a "
                f"policy de isolamento — provavelmente numa migration recente."
            )

        logger.info("RLS verificado: role '%s' está sujeito às policies.", role)
