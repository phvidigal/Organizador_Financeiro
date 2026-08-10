"""Ambiente do Alembic.

Roda em modo síncrono (psycopg) mesmo a aplicação sendo assíncrona (asyncpg):
migration é um script de uma passada só, não ganha nada com concorrência, e a
versão async do env.py traz um bloco de boilerplate que só existe para ser mantido.
A DSN é a mesma, com o driver trocado por `Settings.sync_database_url`.
"""

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.core.config import get_settings
from app.models import Base  # noqa: F401 - popula Base.metadata via app/models/__init__.py

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# TEST_DATABASE_URL permite a suíte aplicar as migrations num banco separado sem
# tocar o de desenvolvimento.
_url = os.getenv("ALEMBIC_DATABASE_URL") or get_settings().sync_database_url
config.set_main_option("sqlalchemy.url", _url.replace("%", "%%"))

target_metadata = Base.metadata


def _include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Mantém fora do autogenerate o que é gerenciado à mão.

    As políticas de RLS e a trigger de `updated_at` não têm representação no
    metadata do SQLAlchemy. Sem este filtro, o autogenerate não as vê e tudo bem —
    mas o `alembic_version` do próprio Alembic seria comparado e proposto para
    remoção em qualquer `--autogenerate` futuro.
    """
    return not (type_ == "table" and name == "alembic_version")


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Sem compare_type, uma troca de NUMERIC(18,2) para NUMERIC(18,4)
            # passa despercebida pelo autogenerate e o schema silenciosamente
            # diverge do modelo.
            compare_type=True,
            compare_server_default=True,
            include_object=_include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
