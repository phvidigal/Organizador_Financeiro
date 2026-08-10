"""Isolamento entre tenants via Row-Level Security.

Toda a suíte roda com a conexão `app_user` — a mesma que a aplicação usa. Rodar
com a conexão de owner daria falso positivo, porque o owner nunca é filtrado
pelas policies.
"""

from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.tenancy import set_tenant_scope
from app.models import TENANT_SCOPED_TABLES

# Mesmo event loop das fixtures de sessão (ver pyproject.toml). Sem isto, o pool
# do asyncpg criado numa fixture de sessão seria usado a partir de outro loop.
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _scoped(engine, tenant_id) -> AsyncSession:
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    return maker()


async def _seed_transaction(admin_session, tenant_id, account_id, external_id) -> None:
    await admin_session.execute(
        text(
            "INSERT INTO transactions "
            "(tenant_id, account_id, source, external_id, amount, kind, date, description_raw) "
            "VALUES (:t, :a, 'PLUGGY', :e, -10.00, 'EXPENSE', '2026-08-01', 'LANCAMENTO')"
        ),
        {"t": str(tenant_id), "a": str(account_id), "e": external_id},
    )
    await admin_session.commit()


async def test_every_tenant_table_has_rls_enabled(admin_session) -> None:
    """Guarda contra a falha mais provável: alguém cria uma tabela com tenant_id
    numa fase futura e esquece a policy. O vazamento seria silencioso; aqui vira
    teste vermelho."""
    rows = (
        await admin_session.execute(
            text(
                "SELECT c.relname, c.relrowsecurity, count(p.polname) AS policies "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "LEFT JOIN pg_policy p ON p.polrelid = c.oid "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                "GROUP BY c.relname, c.relrowsecurity"
            )
        )
    ).all()
    state = {r.relname: (r.relrowsecurity, r.policies) for r in rows}

    for table in TENANT_SCOPED_TABLES:
        enabled, policies = state[table]
        assert enabled, f"RLS desligado em {table}"
        assert policies >= 1, f"{table} sem policy"


async def test_tenant_cannot_read_another_tenants_rows(
    app_engine, admin_session, tenants, account_a
) -> None:
    tenant_a, tenant_b = tenants
    await _seed_transaction(admin_session, tenant_a, account_a, "rls-001")

    session = await _scoped(app_engine, tenant_a)
    async with session, session.begin():
        await set_tenant_scope(session, tenant_a)
        visible = await session.scalar(text("SELECT count(*) FROM transactions"))
        assert visible == 1

    session = await _scoped(app_engine, tenant_b)
    async with session, session.begin():
        await set_tenant_scope(session, tenant_b)
        visible = await session.scalar(text("SELECT count(*) FROM transactions"))
        assert visible == 0


async def test_tenant_cannot_update_or_delete_another_tenants_rows(
    app_engine, admin_session, tenants, account_a
) -> None:
    """RLS filtra escrita, não só leitura. Sem isso, um UPDATE sem WHERE de
    tenant reescreveria a base inteira."""
    tenant_a, tenant_b = tenants
    await _seed_transaction(admin_session, tenant_a, account_a, "rls-002")

    session = await _scoped(app_engine, tenant_b)
    async with session, session.begin():
        await set_tenant_scope(session, tenant_b)
        await session.execute(text("UPDATE transactions SET amount = -999.00"))
        await session.execute(text("DELETE FROM transactions"))

    survived = await admin_session.scalar(
        text("SELECT amount FROM transactions WHERE external_id = 'rls-002'")
    )
    assert survived == Decimal("-10.00")


async def test_insert_with_foreign_tenant_id_is_blocked(
    app_engine, tenants, account_a
) -> None:
    """`WITH CHECK` na policy: sem ele, um tenant escreveria linha marcada com o
    tenant_id de outro — e nunca mais a veria, mas o outro sim."""
    tenant_a, tenant_b = tenants

    session = await _scoped(app_engine, tenant_b)
    async with session:
        # `begin()` explícito, sem context manager: depois do erro a transação
        # está abortada, e o `async with session.begin()` tentaria commit na saída
        # — o teste falharia no commit em vez de na asserção.
        await session.begin()
        await set_tenant_scope(session, tenant_b)
        with pytest.raises(ProgrammingError, match="row-level security"):
            await session.execute(
                text(
                    "INSERT INTO transactions "
                    "(tenant_id, account_id, source, external_id, amount, kind, "
                    "date, description_raw) "
                    "VALUES (:t, :a, 'PLUGGY', 'rls-003', -1.00, 'EXPENSE', "
                    "'2026-08-01', 'FORJADO')"
                ),
                {"t": str(tenant_a), "a": str(account_a)},
            )
        await session.rollback()


async def test_session_without_tenant_scope_sees_nothing(
    app_engine, admin_session, tenants, account_a
) -> None:
    """O modo de falha do RLS é "não vê nada", nunca "vê o tenant errado".

    Uma sessão que escape da dependency `get_tenant_session` fica sem
    `app.tenant_id`, e a comparação com NULL filtra tudo. O bug aparece como tela
    vazia — visível na hora — em vez de vazamento silencioso.
    """
    tenant_a, _ = tenants
    await _seed_transaction(admin_session, tenant_a, account_a, "rls-004")

    session = await _scoped(app_engine, tenant_a)
    async with session, session.begin():
        visible = await session.scalar(text("SELECT count(*) FROM transactions"))
        assert visible == 0


async def test_tenant_scope_does_not_leak_between_transactions(
    app_engine, admin_session, tenants, account_a
) -> None:
    """`SET LOCAL` e não `SET`: o escopo morre com a transação.

    Com pool de conexões, um `SET` de sessão sobreviveria ao request e o próximo
    a reusar a mesma conexão física herdaria o tenant anterior — o pior tipo de
    vazamento, porque depende do acaso de qual conexão o pool devolve.
    """
    tenant_a, _ = tenants
    await _seed_transaction(admin_session, tenant_a, account_a, "rls-005")

    session = await _scoped(app_engine, tenant_a)
    async with session:
        async with session.begin():
            await set_tenant_scope(session, tenant_a)
            assert await session.scalar(text("SELECT count(*) FROM transactions")) == 1

        # Mesma conexão, transação nova, sem set_tenant_scope.
        async with session.begin():
            assert await session.scalar(text("SELECT count(*) FROM transactions")) == 0
