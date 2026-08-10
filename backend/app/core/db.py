"""Engine e sessões assíncronas do SQLAlchemy."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


def _build_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        str(settings.database_url),
        echo=False,
        pool_pre_ping=True,  # descarta conexões mortas depois de o Postgres reiniciar
        pool_size=5,
        max_overflow=10,
    )


engine: AsyncEngine = _build_engine()

SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # objetos continuam legíveis depois do commit
    autoflush=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Sessão sem escopo de tenant.

    Use apenas em endpoints que não tocam dados de tenant (health check,
    metadados públicos). Para qualquer coisa com `tenant_id`, use
    `app.core.tenancy.get_tenant_session`, que ativa o RLS.
    """
    async with SessionLocal() as session:
        yield session
