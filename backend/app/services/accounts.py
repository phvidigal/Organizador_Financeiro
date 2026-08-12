"""Gravação de contas.

Vive aqui e não dentro de `pluggy/` pela mesma razão que `ingestion.py` existe: a
tabela `accounts` tem `bank_connection_id` nullable justamente para permitir conta
manual, criada à mão para importar OFX/CSV. Enterrar o upsert no pacote da Pluggy
obrigaria a importação de arquivo a duplicá-lo.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account

# O que uma re-sincronização pode sobrescrever: são fatos reportados pela
# instituição, e a versão mais recente é sempre a correta. `tenant_id` e
# `pluggy_account_id` ficam de fora porque identificam a linha.
_SYNCABLE_COLUMNS = (
    "bank_connection_id",
    "type",
    "subtype",
    "name",
    "marketing_name",
    "number",
    "balance",
    "currency_code",
    "bank_data",
    "credit_data",
)


async def upsert_pluggy_accounts(
    session: AsyncSession,
    rows: Sequence[dict[str, Any]],
) -> dict[uuid.UUID, tuple[uuid.UUID, str]]:
    """Grava as contas de um item e devolve `{pluggy_account_id: (account_id, type)}`.

    O `type` volta junto porque o mapeamento das transações precisa dele para
    normalizar o sinal em conta de crédito, e buscá-lo de novo seria um SELECT por
    conta logo depois de tê-lo em mãos.

    `deleted_at = NULL` no update pela mesma lógica das transações: se a Pluggy
    voltou a reportar a conta, ela existe. Enquanto o soft delete significar só
    "sumiu na origem", ressuscitar é o comportamento certo.
    """
    if not rows:
        return {}

    stmt = insert(Account).values(list(rows))

    # Mesma disciplina de `upsert_external_transactions`: o `excluded` expõe a
    # tabela inteira, então só entram no UPDATE as colunas que o lote realmente
    # traz. Os mapeadores garantem chaves uniformes (ver `ACCOUNT_ROW_KEYS`).
    present = set(rows[0].keys())
    syncable = [col for col in _SYNCABLE_COLUMNS if col in present]

    stmt = stmt.on_conflict_do_update(
        index_elements=[Account.tenant_id, Account.pluggy_account_id],
        set_={**{col: stmt.excluded[col] for col in syncable}, "deleted_at": None},
    ).returning(Account.id, Account.pluggy_account_id, Account.type)

    result = await session.execute(stmt)
    return {row.pluggy_account_id: (row.id, row.type) for row in result}
