"""Execução do sync em background, dentro do próprio processo.

Separado de `sync.py` de propósito: `sync_connection` é uma corrotina comum,
testável por chamada direta, e esta é a única parte que fala com o event loop.
Misturar as duas tornaria o teste ponta a ponta dependente de agendamento.

Escolha registrada no README: sem Redis, sem worker, sem cron. Um único usuário,
um processo, e o estado durável (`status`, `last_synced_at`, `last_success_at`,
`error`) mora em `bank_connections` — o registro em memória aqui é só o que
permite a interface dizer "sincronizando agora" em vez de "sincronizou às 14h02".

Limitação conhecida: o lock é **por processo**. Com `uvicorn --workers > 1`, dois
syncs simultâneos da mesma conexão passam a ser possíveis. O upsert os torna
inofensivos, mas dobram a cota consumida na Pluggy. O compose roda um worker.
"""

import asyncio
import logging
import uuid

from app.core.tenancy import TenantSessionScope, tenant_session
from app.services.pluggy.client import PluggyClient
from app.services.pluggy.sync import (
    REQUEST_REFRESH_BY_DEFAULT,
    SyncOutcome,
    sync_connection,
)

logger = logging.getLogger(__name__)

# Este dicionário **é** o lock por conexão: enquanto houver task viva para um id,
# um novo disparo é recusado.
_tasks: dict[uuid.UUID, asyncio.Task[SyncOutcome]] = {}
_last_outcome: dict[uuid.UUID, SyncOutcome] = {}


def is_running(connection_id: uuid.UUID) -> bool:
    task = _tasks.get(connection_id)
    return task is not None and not task.done()


def last_outcome(connection_id: uuid.UUID) -> SyncOutcome | None:
    """Resultado da última execução **deste processo**.

    Some no restart, e tudo bem: a parte que importa está no banco, e a interface
    degrada de "sincronizando…" para o estado persistido.
    """
    return _last_outcome.get(connection_id)


def schedule_sync(
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    client: PluggyClient,
    session_scope: TenantSessionScope = tenant_session,
    full: bool = False,
    request_refresh: bool = REQUEST_REFRESH_BY_DEFAULT,
) -> bool:
    """Agenda um sync. Devolve `False` se já houver um em andamento."""
    if is_running(connection_id):
        return False

    task = asyncio.create_task(
        sync_connection(
            tenant_id=tenant_id,
            connection_id=connection_id,
            client=client,
            session_scope=session_scope,
            full=full,
            request_refresh=request_refresh,
        ),
        name=f"pluggy-sync:{connection_id}",
    )

    # Referência forte obrigatória. O event loop guarda apenas uma referência fraca
    # para a task: sem dono, ela pode ser coletada no meio da execução e o sync
    # simplesmente para — sem erro, sem log, de forma intermitente.
    _tasks[connection_id] = task
    task.add_done_callback(_finalize)
    return True


def _finalize(task: asyncio.Task[SyncOutcome]) -> None:
    """Tira a task do registro e garante que a exceção seja vista.

    Sem consumir `task.exception()`, uma falha só apareceria como "Task exception
    was never retrieved" na destruição do objeto — possivelmente minutos depois e
    sem contexto nenhum.
    """
    connection_id = next((cid for cid, t in list(_tasks.items()) if t is task), None)
    if connection_id is not None:
        _tasks.pop(connection_id, None)

    if task.cancelled():
        return

    exc = task.exception()
    if exc is not None:
        logger.error("task de sync terminou com exceção", exc_info=exc)
        return

    if connection_id is not None:
        _last_outcome[connection_id] = task.result()


async def shutdown_sync_tasks(timeout: float = 5.0) -> None:
    """Cancela os syncs em andamento no shutdown.

    Interromper no meio é seguro: cada página já gravada está comitada, e
    `last_success_at` só avança no fim — o próximo disparo relê a mesma janela e o
    upsert absorve a repetição.
    """
    pending = [task for task in _tasks.values() if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait(pending, timeout=timeout)
    _tasks.clear()
