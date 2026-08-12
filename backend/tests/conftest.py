"""Fixtures da suíte.

Duas conexões diferentes, de propósito:

* `admin_engine` / `admin_session` — role owner. Ignora RLS, então serve para
  montar cenário (criar tenants, contas) sem lutar contra as policies.
* `app_engine` / `app_tenant_session` — role `app_user`, o mesmo que a aplicação
  usa. É a única conexão que exercita o RLS de verdade; testar isolamento com a
  conexão de owner daria falso positivo, porque o owner nunca é filtrado.
"""

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alembic import command
from alembic.config import Config
from app.core.tenancy import set_tenant_scope

BACKEND_DIR = Path(__file__).resolve().parent.parent

ADMIN_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@db:5432/finance_test"
)
APP_URL = os.environ.get(
    "TEST_APP_DATABASE_URL", "postgresql+asyncpg://app_user:app_user@db:5432/finance_test"
)

TENANT_A = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
TENANT_B = uuid.UUID("00000000-0000-0000-0000-0000000000bb")


def sync_url(async_url: str) -> str:
    return async_url.replace("+asyncpg", "+psycopg")


def url_for_database(name: str) -> str:
    """DSN administrativa apontando para outro banco.

    `render_as_string(hide_password=False)` e não `str(url)`: o `__str__` do
    SQLAlchemy mascara a senha como `***`, e a DSN resultante falha na
    autenticação com uma mensagem que não sugere em nada a causa.
    """
    return make_url(ADMIN_URL).set(database=name).render_as_string(hide_password=False)


async def create_database(name: str) -> None:
    """Recria o banco do zero. Conecta em `postgres` porque não dá para dropar
    um banco estando conectado nele."""
    admin = make_url(ADMIN_URL).set(database="postgres")
    engine = create_async_engine(admin, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        # WITH (FORCE) derruba conexões penduradas de uma execução anterior que
        # tenha morrido no meio; sem isso o DROP fica bloqueado indefinidamente.
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    await engine.dispose()


async def drop_database(name: str) -> None:
    admin = make_url(ADMIN_URL).set(database="postgres")
    engine = create_async_engine(admin, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    await engine.dispose()


def run_migrations(target_url: str, revision: str = "head") -> None:
    """Aplica migrations num banco arbitrário.

    Passa a DSN por `ALEMBIC_DATABASE_URL`, que é a mesma variável lida pelo
    alembic/env.py em produção — assim o teste exercita o caminho real de
    configuração, e não um atalho só dele.
    """
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = sync_url(target_url)
    command.upgrade(cfg, revision)


def downgrade_migrations(target_url: str, revision: str = "base") -> None:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = sync_url(target_url)
    command.downgrade(cfg, revision)


@pytest.fixture(scope="session")
async def migrated_database() -> AsyncIterator[str]:
    """Banco de teste criado e migrado uma vez por sessão."""
    db_name = make_url(ADMIN_URL).database
    assert db_name is not None
    await create_database(db_name)
    run_migrations(ADMIN_URL)
    yield db_name
    await drop_database(db_name)


@pytest.fixture(scope="session")
async def admin_engine(migrated_database: str):
    engine = create_async_engine(ADMIN_URL)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
async def app_engine(migrated_database: str):
    engine = create_async_engine(APP_URL)
    yield engine
    await engine.dispose()


@pytest.fixture
async def admin_session(admin_engine) -> AsyncIterator[AsyncSession]:
    """Sessão de owner, com rollback ao final.

    A transação nunca é comitada: cada teste começa do estado migrado limpo,
    sem precisar de TRUNCATE entre um e outro.
    """
    maker = async_sessionmaker(bind=admin_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
        await session.rollback()


@pytest.fixture
async def tenants(admin_session: AsyncSession) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """Dois tenants com dado próprio — a base do teste de isolamento.

    Comitado de verdade (e não deixado na transação do teste) porque a sessão
    `app_session` abre outra conexão e não enxergaria dado não comitado.
    """
    await admin_session.execute(
        text("INSERT INTO tenants (id, name) VALUES (:a, 'Tenant A'), (:b, 'Tenant B')"),
        {"a": str(TENANT_A), "b": str(TENANT_B)},
    )
    await admin_session.commit()
    yield TENANT_A, TENANT_B
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": str(TENANT_A), "b": str(TENANT_B)}
    )
    await admin_session.commit()


@pytest.fixture
def app_tenant_session(app_engine):
    """Espelho de `app.core.tenancy.tenant_session` apontado para o banco de teste.

    Existe porque `tenant_session` está preso ao `SessionLocal` de produção, e
    serviços que rodam fora do request (o sync da Pluggy) recebem o abridor de
    sessão por parâmetro justamente para poderem ser testados.

    Conecta como `app_user` de propósito, e não como owner: o sync roda sob RLS em
    produção, e testar com a conexão de owner esconderia uma policy faltando ou um
    `set_tenant_scope` esquecido na task de background — o modo de falha silencioso
    em que a leitura devolve zero linhas.

    Comita ao sair do bloco (é o que `session.begin()` faz), ao contrário de
    `admin_session`: o serviço sob teste depende de o dado gravado numa unidade de
    trabalho estar visível para a seguinte.
    """
    maker = async_sessionmaker(
        bind=app_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )

    @asynccontextmanager
    async def scope(tenant_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
        async with maker() as session, session.begin():
            await set_tenant_scope(session, tenant_id)
            yield session

    return scope


@pytest.fixture
async def account_a(admin_session: AsyncSession, tenants) -> uuid.UUID:
    """Conta corrente do tenant A."""
    tenant_a, _ = tenants
    account_id = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO accounts (id, tenant_id, type, subtype, name) "
            "VALUES (:id, :tenant_id, 'BANK', 'CHECKING_ACCOUNT', 'Conta Corrente')"
        ),
        {"id": str(account_id), "tenant_id": str(tenant_a)},
    )
    await admin_session.commit()
    return account_id
