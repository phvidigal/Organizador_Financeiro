"""Listagem de transações e a correção manual de categoria."""

import uuid
from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import get_tenant_session
from app.models.categorization_review import CategorizationReview
from app.models.category_rule import CategoryRule
from app.models.enums import CategorizationStatus, CategorySource, MatchType, TransactionKind
from app.models.transaction import Transaction
from app.schemas.common import Page
from app.schemas.transaction import (
    TransactionCategorizedRead,
    TransactionCategorizeRequest,
    TransactionRead,
)
from app.services.categorization.catalog import CategoryCatalog, load_catalog
from app.services.categorization.decide import question_for
from app.services.categorization.rules import (
    MIN_PATTERN_LENGTH,
    Rule,
    apply_rules,
    normalize_pattern,
    rule_matches,
    suggest_pattern,
)
from app.services.categorization.store import apply_manual_decision

router = APIRouter(prefix="/transactions", tags=["transactions"])

MAX_LIMIT = 200


def _escape_like(term: str) -> str:
    """`%` e `_` literais: buscar "50%" não pode virar "qualquer coisa"."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def to_read(transaction: Transaction, catalog: CategoryCatalog) -> TransactionRead:
    """A linha com os dois campos calculados no servidor."""
    read = TransactionRead.model_validate(transaction)
    read.review_question = question_for(
        status=transaction.categorization_status,
        category_id=transaction.category_id,
        counterparty_is_holder=transaction.counterparty_is_holder,
        catalog=catalog,
    )
    read.suggested_rule_pattern = suggest_pattern(transaction.description_raw)
    return read


@router.get("", response_model=Page[TransactionRead])
async def list_transactions(
    account_id: uuid.UUID | None = Query(None),
    category_id: uuid.UUID | None = Query(None),
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
    q: str | None = Query(
        None, max_length=100, description="trecho da descrição, sem diferenciar caixa"
    ),
    # Transferência entre contas próprias é ruído na leitura do dia a dia — o
    # dinheiro não saiu de casa. A tela pede para escondê-la e oferece o caminho de
    # volta; o padrão da API continua sendo mostrar tudo.
    hide_own_transfers: bool = Query(
        False, description="omite 'Transferências > Transferência entre contas próprias'"
    ),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_tenant_session),
) -> Page[TransactionRead]:
    """Extrato paginado, do mais recente para o mais antigo.

    O `total` sai de um `count()` separado porque a interface precisa saber quando
    parar de paginar, e a tabela é pequena o bastante para isso não pesar. Os
    filtros vão todos para o SQL, e não para depois da página: senão o total e a
    paginação passariam a mentir.
    """
    catalog = await load_catalog(session)

    filters = [Transaction.deleted_at.is_(None)]
    if account_id is not None:
        filters.append(Transaction.account_id == account_id)
    if category_id is not None:
        filters.append(Transaction.category_id == category_id)
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
    if q is not None and q.strip():
        filters.append(
            Transaction.description_raw.ilike(f"%{_escape_like(q.strip())}%", escape="\\")
        )
    own = catalog.own_transfer
    if hide_own_transfers and own is not None:
        # `IS DISTINCT FROM` e não `!=`: com `!=`, toda linha de `category_id` NULL
        # sairia junto (`NULL != x` é NULL, não verdadeiro) — e o que sumiria seria
        # justamente o que ainda não foi categorizado.
        filters.append(Transaction.category_id.is_distinct_from(own.id))

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
        items=[to_read(row, catalog) for row in result],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.patch("/{transaction_id}", response_model=TransactionCategorizedRead)
async def categorize_transaction(
    transaction_id: uuid.UUID,
    payload: TransactionCategorizeRequest,
    session: AsyncSession = Depends(get_tenant_session),
) -> TransactionCategorizedRead:
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

    # A regra é validada **antes** de qualquer escrita: um 400 aqui não pode deixar
    # a correção gravada pela metade.
    pattern: str | None = None
    if payload.rule_pattern is not None:
        pattern = normalize_pattern(payload.rule_pattern)
        if pattern is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"o trecho da regra precisa de pelo menos {MIN_PATTERN_LENGTH} caracteres",
            )
        if kind != entry.kind:
            # A regra grava a categoria e herda a natureza dela. Com o `kind`
            # sobreposto, os próximos lançamentos sairiam diferentes deste.
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "uma regra herda a natureza da categoria; desfaça a troca de natureza "
                "ou corrija sem criar regra",
            )
        probe = Rule(
            id=uuid.uuid4(),
            match_type=MatchType.CONTAINS.value,
            pattern=pattern,
            category_id=entry.id,
        )
        if not rule_matches(probe, transaction.description_raw):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "o trecho não aparece na descrição deste lançamento",
            )

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

    # Depois da correção, e não antes: esta linha já é MANUAL, e a regra não a toca.
    # Vale na hora para o que o LLM decidiu e para o que ainda está na fila — é para
    # isso que o titular a criou.
    rule_id: uuid.UUID | None = None
    applied: int | None = None
    if pattern is not None:
        rule = CategoryRule(
            tenant_id=transaction.tenant_id,
            match_type=MatchType.CONTAINS.value,
            pattern=pattern,
            category_id=entry.id,
        )
        session.add(rule)
        await session.flush()
        rule_id = rule.id
        applied = await apply_rules(
            session,
            rules=[
                Rule(id=rule.id, match_type=rule.match_type, pattern=pattern, category_id=entry.id)
            ],
            catalog=catalog,
            include_llm=True,
        )

    # O objeto em memória ainda tem os valores antigos: o UPDATE foi emitido em SQL,
    # não pelo ORM. Sem o refresh, a resposta mostraria o estado anterior à correção
    # e a tela pintaria a linha como se nada tivesse acontecido.
    await session.refresh(transaction)
    read = to_read(transaction, catalog)
    return TransactionCategorizedRead(
        **read.model_dump(), rule_id=rule_id, rule_applied=applied
    )
