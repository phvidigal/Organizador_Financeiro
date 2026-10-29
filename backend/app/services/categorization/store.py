"""A única escrita das colunas de categorização.

Existe pelo mesmo motivo que `app/services/ingestion.py`: as colunas andam juntas,
e espalhar `UPDATE transactions SET category_id = …` pelo código é exatamente como
o `kind` herdado se perde. Uma camada nova da pipeline (regra, embedding) deve
passar por aqui em vez de escrever a sua própria versão.

São sete colunas numa tacada só:

    category_id · category_source · categorization_status
    category_confidence · categorized_at · kind · category_rule_id

`kind` é a que se esquece, e é a que importa para o dashboard: sem herdá-la de
`categories.kind`, um Pix classificado como "Transferências" continua contando
como gasto. `category_rule_id` é a outra: uma linha que o LLM ou o titular
redecide e continua apontando para a regra antiga seria devolvida à fila quando
essa regra fosse desativada — desfazendo uma decisão que não era dela.

São três caminhos de escrita, e a diferença entre eles não é cosmética:
`apply_decision` grava a decisão do LLM, `apply_rule_decision` a de uma regra, e
`apply_manual_decision` a do titular. O último zera `category_confidence` e
depende de o chamador ter registrado a linha de `categorization_reviews` — ver o
docstring de lá.

**Precedência:** MANUAL > evidência de conta própria > regra do titular > LLM. Os
caminhos automáticos repetem `category_source IS DISTINCT FROM 'MANUAL'` no próprio
UPDATE, e não só no chamador: é a guarda que não depende de ninguém lembrar.
"""

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import case, or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import CategorizationStatus, CategorySource, TransactionKind
from app.models.transaction import Transaction
from app.services.categorization.catalog import CategoryEntry
from app.services.categorization.decide import Decision

logger = logging.getLogger(__name__)

NOT_MANUAL = Transaction.category_source.is_distinct_from(CategorySource.MANUAL.value)

# O `kind` de uma linha que volta para a fila: o do sinal, como `derive_kind` de
# `ingestion.py` — é o estado em que ela estava antes de qualquer categorização.
_KIND_FROM_SIGN = case(
    (Transaction.amount >= 0, TransactionKind.INCOME.value),
    else_=TransactionKind.EXPENSE.value,
)

# Os valores que devolvem uma linha à fila, num lugar só.
_BACK_TO_QUEUE: dict[str, object] = {
    "category_id": None,
    "category_source": None,
    "category_rule_id": None,
    "categorization_status": CategorizationStatus.PENDING.value,
    "category_confidence": None,
    "categorized_at": None,
    "kind": _KIND_FROM_SIGN,
}


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
        "category_rule_id": None,
    }

    # `kind=None` significa "não mexa": é o caso da resposta que não resolveu para
    # nenhuma categoria. Sobrescrever com o valor derivado do sinal apagaria uma
    # eventual marcação anterior sem nada em troca.
    if decision.kind is not None:
        values["kind"] = decision.kind

    await session.execute(
        update(Transaction).where(Transaction.id == transaction_id).values(**values)
    )


async def apply_manual_decision(
    session: AsyncSession,
    *,
    transaction_id: uuid.UUID,
    category_id: uuid.UUID,
    kind: str,
    now: datetime | None = None,
) -> None:
    """Grava a correção do titular. Irmã de `apply_decision`, e mora aqui pelo mesmo
    motivo: as seis colunas andam juntas, e quem escreve o próprio `UPDATE` esquece
    o `kind`.

    Duas diferenças em relação ao caminho do LLM:

    * o status é sempre `CATEGORIZED`. Não existe "revisão que precisa de revisão" —
      `NEEDS_REVIEW` é o pedido de resposta, e esta função é a resposta;
    * **`category_confidence` é zerada.** Confiança é a autoavaliação de um modelo;
      uma escolha humana não tem análogo, e deixar o número anterior faria a coluna
      significar coisas diferentes conforme o `category_source` — "quão certo o LLM
      estava" em linhas MANUAL e "quão certo ele está" nas demais.

    Zerar só é seguro porque o chamador grava antes uma linha em
    `categorization_reviews` com `previous_confidence`. **A medição de calibração
    passa a se ler de lá**, não de `transactions.category_confidence`. Sem a linha
    de review, este `None` destruiria exatamente o dado que a Fase 3 deixou pendente.

    O `kind` vem do chamador, e não de `categories.kind` como no caminho do LLM,
    porque a tela de revisão permite sobrepô-lo: um Pix enviado para pagar um
    serviço é despesa mesmo apontando para uma categoria TRANSFER, e só o titular
    sabe disso. Herdar o `kind` da categoria continua sendo o default — quem decide
    é `app/api/v1/transactions.py`.
    """
    await session.execute(
        update(Transaction)
        .where(Transaction.id == transaction_id)
        .values(
            category_id=category_id,
            category_source=CategorySource.MANUAL.value,
            categorization_status=CategorizationStatus.CATEGORIZED.value,
            category_confidence=None,
            categorized_at=now or datetime.now(UTC),
            kind=kind,
            category_rule_id=None,
        )
    )


async def apply_rule_decision(
    session: AsyncSession,
    *,
    transaction_ids: Sequence[uuid.UUID],
    category: CategoryEntry,
    rule_id: uuid.UUID | None,
    now: datetime | None = None,
) -> int:
    """Grava a decisão de uma regra num lote de transações. Nunca toca em MANUAL.

    `rule_id = None` é a regra embutida de conta própria (evidência de
    `counterparty_is_holder`); as demais vêm de `category_rules`.

    Status `CATEGORIZED` e confiança NULL: uma regra não tem autoavaliação, e a
    coluna `category_confidence` continua significando uma coisa só — o número cru
    do LLM. O `kind` é herdado da categoria (invariante 1b).
    """
    if not transaction_ids:
        return 0
    result = await session.execute(
        update(Transaction)
        .where(Transaction.id.in_(list(transaction_ids)), NOT_MANUAL)
        .values(
            category_id=category.id,
            category_source=CategorySource.RULE.value,
            categorization_status=CategorizationStatus.CATEGORIZED.value,
            category_confidence=None,
            categorized_at=now or datetime.now(UTC),
            kind=category.kind,
            category_rule_id=rule_id,
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


async def apply_own_transfer_evidence(
    session: AsyncSession,
    *,
    category: CategoryEntry,
    now: datetime | None = None,
) -> tuple[int, int]:
    """A regra embutida de conta própria, nos dois sentidos. Devolve (promovidas, devolvidas).

    **Promove** toda linha cuja contraparte é o titular e que ainda não está nela —
    inclusive decisões antigas do LLM e de regras do titular: evidência vem antes de
    palpite e de padrão de texto.

    **Devolve à fila** a linha que esta regra decidiu e cuja evidência sumiu (a
    origem corrigiu quem recebeu o Pix). Sem isso, o upsert do sync — que preserva
    toda linha já decidida — a deixaria fora dos totais para sempre.
    """
    decided_by_evidence = (
        (Transaction.category_source == CategorySource.RULE.value)
        & Transaction.category_rule_id.is_(None)
    )
    # "Ainda não está nela", escrito sem `NOT (...)`: numa linha PENDING as colunas
    # de categoria são NULL, e `NOT (NULL AND NULL)` é NULL — a linha sumiria
    # justamente do caso mais comum. `IS DISTINCT FROM` não tem esse buraco.
    not_yet_there = or_(
        Transaction.category_source.is_distinct_from(CategorySource.RULE.value),
        Transaction.category_rule_id.is_not(None),
        Transaction.category_id.is_distinct_from(category.id),
    )

    promoted = await session.execute(
        update(Transaction)
        .where(
            Transaction.counterparty_is_holder.is_(True),
            Transaction.deleted_at.is_(None),
            NOT_MANUAL,
            not_yet_there,
        )
        .values(
            category_id=category.id,
            category_source=CategorySource.RULE.value,
            categorization_status=CategorizationStatus.CATEGORIZED.value,
            category_confidence=None,
            categorized_at=now or datetime.now(UTC),
            kind=category.kind,
            category_rule_id=None,
        )
        .execution_options(synchronize_session=False)
    )

    demoted = await session.execute(
        update(Transaction)
        .where(decided_by_evidence, Transaction.counterparty_is_holder.is_not(True))
        .values(**_BACK_TO_QUEUE)
        .execution_options(synchronize_session=False)
    )

    return promoted.rowcount or 0, demoted.rowcount or 0


async def reset_rule_decisions(session: AsyncSession, *, rule_id: uuid.UUID) -> int:
    """Devolve à fila o que uma regra do titular decidiu — e só isso.

    É o desfazer de uma regra desativada. A linha que alguém redecidiu depois já não
    aponta para ela (`category_rule_id` é limpo em toda escrita), então não volta.
    """
    result = await session.execute(
        update(Transaction)
        .where(
            Transaction.category_source == CategorySource.RULE.value,
            Transaction.category_rule_id == rule_id,
        )
        .values(**_BACK_TO_QUEUE)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


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
        .values(**_BACK_TO_QUEUE)
    )
    affected = result.rowcount or 0
    logger.info("reset de categorização (%s): %s transação(ões)", source.value, affected)
    return affected
