"""A única escrita das colunas de categorização.

Existe pelo mesmo motivo que `app/services/ingestion.py`: as colunas andam juntas,
e espalhar `UPDATE transactions SET category_id = …` pelo código é exatamente como
o `kind` herdado se perde. Uma camada nova da pipeline (regra, embedding) deve
passar por aqui em vez de escrever a sua própria versão.

São seis colunas numa tacada só:

    category_id · category_source · categorization_status
    category_confidence · categorized_at · kind

`kind` é a que se esquece, e é a que importa para o dashboard: sem herdá-la de
`categories.kind`, um Pix classificado como "Transferências" continua contando
como gasto.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import case, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import CategorizationStatus, CategorySource, TransactionKind
from app.models.transaction import Transaction
from app.services.categorization.decide import Decision

logger = logging.getLogger(__name__)


async def apply_decision(
    session: AsyncSession,
    *,
    transaction_id: uuid.UUID,
    decision: Decision,
    now: datetime | None = None,
) -> None:
    """Grava a decisão do LLM numa transação.

    `category_source` fica **NULL** quando o resultado é `FAILED`, e é deliberado:
    o upsert de re-sincronização preserva toda linha com `category_source IS NOT
    NULL`, então gravar `'LLM'` num fracasso congelaria a linha em FAILED para
    sempre. Com NULL, o próximo sync a devolve para `PENDING` e ela é tentada de
    novo sozinha.

    `NEEDS_REVIEW` é o oposto: houve decisão, ela só precisa de um humano. Fica com
    `category_source = 'LLM'` e é preservada.
    """
    failed = decision.status == CategorizationStatus.FAILED

    values: dict[str, object] = {
        "category_id": decision.category_id,
        "category_source": None if failed else CategorySource.LLM.value,
        "categorization_status": decision.status.value,
        "category_confidence": decision.confidence,
        "categorized_at": None if failed else (now or datetime.now(UTC)),
    }

    # `kind=None` significa "não mexa": é o caso da resposta que não resolveu para
    # nenhuma categoria. Sobrescrever com o valor derivado do sinal apagaria uma
    # eventual marcação anterior sem nada em troca.
    if decision.kind is not None:
        values["kind"] = decision.kind

    await session.execute(
        update(Transaction).where(Transaction.id == transaction_id).values(**values)
    )


async def reset_categorization(
    session: AsyncSession,
    *,
    source: CategorySource = CategorySource.LLM,
) -> int:
    """Devolve à fila as transações decididas por uma origem. Nunca toca em `MANUAL`.

    É o que torna a iteração de prompt barata: mudou o prompt, reseta as linhas do
    LLM e roda de novo. Sem isto, a única forma de reprocessar seria um `UPDATE` na
    mão — e quem escreve esse UPDATE esquece de reverter o `kind`, deixando um Pix
    marcado como TRANSFER apontando para categoria nenhuma.

    O `kind` volta a ser derivado do sinal do valor, espelhando `derive_kind` de
    `ingestion.py`: é o estado em que a linha estava antes de qualquer
    categorização.
    """
    if source == CategorySource.MANUAL:
        # A correção do usuário é a régua para medir o acerto do LLM e a base da
        # futura pipeline de regras. Nenhum caminho do sistema pode apagá-la — é a
        # mesma razão pela qual o upsert do sync a preserva.
        raise ValueError("reset de category_source='MANUAL' não é permitido")

    result = await session.execute(
        update(Transaction)
        .where(Transaction.category_source == source.value)
        .values(
            category_id=None,
            category_source=None,
            categorization_status=CategorizationStatus.PENDING.value,
            category_confidence=None,
            categorized_at=None,
            kind=case(
                (Transaction.amount >= 0, TransactionKind.INCOME.value),
                else_=TransactionKind.EXPENSE.value,
            ),
        )
    )
    affected = result.rowcount or 0
    logger.info("reset de categorização (%s): %s transação(ões)", source.value, affected)
    return affected
