"""Schemas de conexão bancária e de sincronização."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AdoptItemRequest(BaseModel):
    """Adoção de um item que já existe na Pluggy.

    `uuid.UUID` e não `str`: a coluna é `PgUUID NOT NULL`, então um valor mal
    formado vira 422 com mensagem clara antes de chegar ao banco — em vez de um
    500 vindo do driver.

    É o caminho principal de cadastro, e não um atalho: `GET /items` não permite
    listagem (responde 401 sempre), então não há como o backend descobrir sozinho
    quais conexões existem. Quem cria o item guarda o id.
    """

    item_id: uuid.UUID = Field(
        description="itemId de uma conexão já criada em meu.pluggy.ai",
        examples=["0a1b2c3d-4e5f-6789-abcd-ef0123456789"],
    )


class BankConnectionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    pluggy_item_id: uuid.UUID
    connector_id: int | None
    connector_name: str | None
    status: str
    execution_status: str | None
    products: list[str] | None
    last_synced_at: datetime | None
    last_success_at: datetime | None
    # Quando a Pluggy vai coletar dado novo sozinha. No tier pessoal é a única
    # resposta honesta para "quando vou ver lançamento novo?", já que não dá para
    # pedir atualização — ver docs/pluggy-api.md.
    next_auto_sync_at: datetime | None
    consent_expires_at: datetime | None
    error: dict[str, Any] | None


class CategoryMapReportRead(BaseModel):
    """Relatório do de/para de categorias.

    `unmatched_pluggy` e `unmapped_local` não são erro: são o dump que permite
    completar `PLUGGY_TO_LOCAL` quando a taxonomia de um dos lados mudar.
    """

    mapped: int
    already_mapped: int
    unmatched_pluggy: list[str]
    unmapped_local: list[str]
    conflicts: list[str]


class SyncOutcomeRead(BaseModel):
    """Resultado da última execução **neste processo**.

    Some no restart do backend, e tudo bem: o que é durável está na própria
    conexão. Serve para a tela mostrar "12 transações novas" logo após um sync.
    """

    status: str
    started_at: datetime
    finished_at: datetime | None
    accounts_synced: int
    transactions_upserted: int
    item_refresh_supported: bool | None
    warnings: list[str]
    error: dict[str, Any] | None
    categories: CategoryMapReportRead | None


class SyncStatusRead(BaseModel):
    """Estado do sync de uma conexão — o que a interface põe em polling."""

    connection_id: uuid.UUID
    # Vem do registro em memória do runner, não do banco.
    running: bool
    # Estado durável, vindo de `bank_connections`.
    status: str
    execution_status: str | None
    last_synced_at: datetime | None
    last_success_at: datetime | None
    next_auto_sync_at: datetime | None
    consent_expires_at: datetime | None
    error: dict[str, Any] | None

    # Preenchido quando o disparo foi recusado pelo throttle, para a tela dizer
    # "sincronizado há pouco" em vez de tratar como falha.
    throttled: bool = False
    retry_after_seconds: int | None = None

    last_outcome: SyncOutcomeRead | None = None
