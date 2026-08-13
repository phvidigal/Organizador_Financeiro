"""Execução da categorização em background, dentro do próprio processo.

Separado de `job.py` pelo mesmo motivo que `pluggy/runner.py` é separado de
`sync.py`: `categorize_pending` é uma corrotina comum, testável por chamada direta,
e esta é a única parte que fala com o event loop.

O lock é **por tenant**, não por conexão bancária: a fila é a tabela inteira do
tenant, e duas execuções simultâneas categorizariam as mesmas linhas duas vezes —
inofensivo para o banco (a última escrita vence) e caro na GPU.

Limitação conhecida, igual à do sync: o lock vive na memória do processo. Com
`uvicorn --workers > 1` ele deixa de valer. O compose roda um worker.
"""

import asyncio
import logging
import uuid

from app.core.tenancy import TenantSessionScope, tenant_session
from app.services.categorization.client import OllamaClient
from app.services.categorization.job import CategorizationOutcome, categorize_pending

logger = logging.getLogger(__name__)

# Este dicionário **é** o lock por tenant: enquanto houver task viva, um novo
# disparo é recusado.
_tasks: dict[uuid.UUID, asyncio.Task[CategorizationOutcome]] = {}
_last_outcome: dict[uuid.UUID, CategorizationOutcome] = {}


def is_running(tenant_id: uuid.UUID) -> bool:
    task = _tasks.get(tenant_id)
    return task is not None and not task.done()


def last_outcome(tenant_id: uuid.UUID) -> CategorizationOutcome | None:
    """Resultado da última execução **deste processo**.

    Some no restart, e tudo bem: o que importa está em `transactions`, e a interface
    degrada de "categorizando…" para a contagem da fila.
    """
    return _last_outcome.get(tenant_id)


def schedule_categorization(
    *,
    tenant_id: uuid.UUID,
    client: OllamaClient,
    session_scope: TenantSessionScope = tenant_session,
    limit: int | None = None,
) -> bool:
    """Agenda uma execução. Devolve `False` se já houver uma em andamento."""
    if is_running(tenant_id):
        return False

    task = asyncio.create_task(
        categorize_pending(
            tenant_id=tenant_id,
            client=client,
            session_scope=session_scope,
            limit=limit,
        ),
        name=f"categorization:{tenant_id}",
    )

    # Referência forte obrigatória. O event loop guarda apenas uma referência fraca
    # para a task: sem dono, ela pode ser coletada no meio da execução e o job
    # simplesmente para — sem erro, sem log, de forma intermitente.
    _tasks[tenant_id] = task
    task.add_done_callback(_finalize)
    return True


def _finalize(task: asyncio.Task[CategorizationOutcome]) -> None:
    """Tira a task do registro e garante que a exceção seja vista.

    Sem consumir `task.exception()`, uma falha só apareceria como "Task exception
    was never retrieved" na destruição do objeto — possivelmente minutos depois e
    sem contexto nenhum.
    """
    tenant_id = next((tid for tid, t in list(_tasks.items()) if t is task), None)
    if tenant_id is not None:
        _tasks.pop(tenant_id, None)

    if task.cancelled():
        return

    exc = task.exception()
    if exc is not None:
        logger.error("task de categorização terminou com exceção", exc_info=exc)
        return

    if tenant_id is not None:
        _last_outcome[tenant_id] = task.result()


async def shutdown_categorization_tasks(timeout: float = 5.0) -> None:
    """Cancela as execuções em andamento no shutdown.

    Interromper no meio é seguro: cada transação já categorizada está comitada, e as
    que faltam continuam PENDING — o próximo disparo pega exatamente de onde parou.
    """
    pending = [task for task in _tasks.values() if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait(pending, timeout=timeout)
    _tasks.clear()
