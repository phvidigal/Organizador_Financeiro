"""Listagem de transações e a correção manual de categoria."""

import uuid
from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import get_tenant_session
from app.models.categorization_review import CategorizationReview
from app.models.enums import CategorizationStatus, CategorySource, TransactionKind
from app.models.transaction import Transaction
from app.schemas.common import Page
from app.schemas.transaction import TransactionCategorizeRequest, TransactionRead
from app.services.categorization.catalog import load_catalog
from app.services.categorization.store import apply_manual_decision

router = APIRouter(prefix="/transactions", tags=["transactions"])

MAX_LIMIT = 200


@router.get("", response_model=Page[TransactionRead])
async def list_transactions(
    account_id: uuid.UUID | None = Query(None),
    category_id: uuid.UUID | None = Query(None),
    # Existe para o extrato poder esconder "Transferência entre contas próprias" —
    # dinheiro andando entre contas do titular é ruído na leitura do dia a dia.
    #
    # Exclusão por id e não por natureza: `kind = TRANSFER` também pega pagamento de
    # fatura e aplicação em investimento, que são movimentos que o titular quer ver.
    exclude_category_id: uuid.UUID | None = Query(
        None, description="omite uma categoria da listagem"
    ),
    date_from: date_type | None = Query(None),
    date_to: date_type | None = Query(None),
    # Enums e não `str`: com `str`, um valor inválido devolvia lista vazia em
    # silêncio, e "não há transação nesse filtro" é indistinguível de "o filtro
    # está escrito errado". Com o enum vira 422 apontando o campo.
    kind: TransactionKind | None = Query(None),
    categorization_status: CategorizationStatus | None = Query(None),
    category_source: CategorySource | None = Query(
        None, description="quem decidiu a categoria; MANUAL isola as correções do titular"
    ),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_tenant_session),
) -> Page[TransactionRead]:
    """Extrato paginado, do mais recente para o mais antigo.

    O `total` sai de um `count()` separado porque a interface precisa saber quando
    parar de paginar, e a tabela é pequena o bastante para isso não pesar.
    """
    filters = [Transaction.deleted_at.is_(None)]
    if account_id is not None:
        filters.append(Transaction.account_id == account_id)
    if category_id is not None:
        filters.append(Transaction.category_id == category_id)
    if exclude_category_id is not None:
        # `IS DISTINCT FROM` e não `!=`: com `!=`, toda linha de `category_id` NULL
        # sairia da listagem junto, porque `NULL != x` é NULL e não verdadeiro — e o
        # que sumiria seria justamente o que ainda não foi categorizado.
        filters.append(Transaction.category_id.is_distinct_from(exclude_category_id))
    if date_from is not None:
        filters.append(Transaction.date >= date_from)
    if date_to is not None:
        filters.append(Transaction.date <= date_to)
    if kind is not None:
        filters.append(Transaction.kind == kind.value)
    if categorization_status is not None:
        filters.append(Transaction.categorization_status == categorization_status.value)
    if category_source is not None:
        filters.append(Transaction.category_source == category_source.value)

    total = await session.scalar(select(func.count()).select_from(Transaction).where(*filters)) or 0

    result = await session.scalars(
        select(Transaction)
        .where(*filters)
        # `created_at` desempata: várias transações no mesmo dia sem critério
        # secundário sairiam em ordem indefinida, e a paginação repetiria ou
        # pularia linhas entre uma página e outra.
        .order_by(Transaction.date.desc(), Transaction.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    return Page[TransactionRead](
        items=[TransactionRead.model_validate(row) for row in result],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.patch("/{transaction_id}", response_model=TransactionRead)
async def categorize_transaction(
    transaction_id: uuid.UUID,
    payload: TransactionCategorizeRequest,
    session: AsyncSession = Depends(get_tenant_session),
) -> TransactionRead:
    """Correção manual de categoria — o endpoint que produz `category_source='MANUAL'`.

    É o único caminho do sistema que gera a régua com que o acerto do LLM é medido,
    a base da futura pipeline de regras e a resposta às perguntas que o modelo faz
    baixando a confiança. Por isso ele **registra antes de sobrescrever**: a linha
    de `categorization_reviews` guarda a categoria e a confiança anteriores, sem as
    quais não dá nem para saber se o titular confirmou a escolha do LLM ou a
    corrigiu.

    O registro e a escrita ficam na mesma transação de banco — a sessão de
    `get_tenant_session` já abre uma. Se o UPDATE falhar, a revisão não fica
    registrada sozinha; se o INSERT falhar, a confiança não é perdida.

    `kind` é opcional e herda de `categories.kind` quando ausente (invariante 4). O
    override existe porque a categoria não decide tudo: um Pix enviado para pagar um
    serviço é despesa, mesmo apontando para uma categoria TRANSFER, e só o titular
    sabe disso.
    """
    transaction = await session.get(Transaction, transaction_id)
    if transaction is None or transaction.deleted_at is not None:
        # RLS já faz transação de outro tenant não existir para esta sessão, então
        # o mesmo 404 cobre "não é sua" sem revelar que ela existe.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "transação não encontrada")

    catalog = await load_catalog(session)
    entry = catalog.by_id.get(payload.category_id)
    if entry is None:
        # Categoria de outro tenant, inexistente ou desativada — os três casos
        # chegam aqui iguais, e nenhum deve ser aceito: gravar uma categoria
        # desativada a traria de volta pela porta dos fundos.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "categoria desconhecida, desativada ou de outro titular",
        )

    kind = payload.kind.value if payload.kind is not None else entry.kind

    session.add(
        CategorizationReview(
            tenant_id=transaction.tenant_id,
            transaction_id=transaction.id,
            previous_category_id=transaction.category_id,
            previous_kind=transaction.kind,
            previous_source=transaction.category_source,
            previous_status=transaction.categorization_status,
            previous_confidence=transaction.category_confidence,
            new_category_id=entry.id,
            new_kind=kind,
        )
    )
    # Flush explícito porque a `SessionLocal` é `autoflush=False`: sem ele o INSERT
    # só sairia no commit, que acontece no teardown da dependency — depois de a
    # resposta 200 já ter sido montada. Uma falha ali (a policy de RLS, o GRANT que
    # falta) viraria erro fora do handler, com a tela mostrando sucesso.
    await session.flush()

    await apply_manual_decision(
        session,
        transaction_id=transaction.id,
        category_id=entry.id,
        kind=kind,
    )

    # O objeto em memória ainda tem os valores antigos: o UPDATE foi emitido em SQL,
    # não pelo ORM. Sem o refresh, a resposta mostraria o estado anterior à correção
    # e a tela pintaria a linha como se nada tivesse acontecido.
    await session.refresh(transaction)
    return TransactionRead.model_validate(transaction)
