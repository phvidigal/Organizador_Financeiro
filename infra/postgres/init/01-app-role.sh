#!/bin/bash
# Cria o role da aplicação no primeiro boot do container.
#
# Precisa acontecer aqui, e não numa migration, por ordem de dependência: a
# aplicação sobe junto com o compose e já tenta conectar como app_user, enquanto
# a migration é um comando manual que roda depois. Criar o role na migration
# deixaria o backend sem conseguir abrir conexão nenhuma até alguém rodar
# `alembic upgrade head`.
#
# ATENÇÃO: scripts em /docker-entrypoint-initdb.d/ só rodam quando o diretório de
# dados está vazio. Mudou APP_DB_PASSWORD depois do primeiro `up`? O role continua
# com a senha antiga — altere com ALTER ROLE ou derrube o volume (`down -v`).
set -euo pipefail

: "${APP_DB_USER:?APP_DB_USER não definido}"
: "${APP_DB_PASSWORD:?APP_DB_PASSWORD não definido}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    -- Sem CREATEDB, sem SUPERUSER e, principalmente, sem BYPASSRLS:
    -- qualquer um dos três anularia as políticas de isolamento por tenant.
    CREATE ROLE "${APP_DB_USER}" LOGIN PASSWORD '${APP_DB_PASSWORD}' NOSUPERUSER NOCREATEDB NOCREATEROLE;

    GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO "${APP_DB_USER}";
    GRANT USAGE ON SCHEMA public TO "${APP_DB_USER}";
EOSQL

echo "Role ${APP_DB_USER} criado."
