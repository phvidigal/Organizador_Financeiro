"""As regras do titular: listar, desativar, reativar.

Criar não mora aqui: uma regra nasce de uma resposta, em `PATCH /transactions/{id}`
com `rule_pattern`, porque é ali que o titular acabou de confirmar que o trecho e a
categoria andam juntos. Uma tela de "nova regra" solta convidaria a padrão que
nunca foi conferido contra lançamento nenhum.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import get_tenant_session
from app.models.category_rule import CategoryRule
from app.models.enums import CategorySource
from app.models.transaction import Transaction
from app.schemas.rule import RuleRead, RuleUpdatedRead, RuleUpdateRequest
from app.services.categorization.catalog import CategoryCatalog, load_catalog
from app.services.categorization.rules import Rule, apply_rules
from app.services.categorization.store import reset_rule_decisions

router = APIRouter(prefix="/rules", tags=["rules"])


async def _applied_counts(session: AsyncSession) -> dict[uuid.UUID, int]:
    result = await session.execute(
        select(Transaction.category_rule_id, func.count())
        .where(
            Transaction.category_source == CategorySource.RULE.value,
            Transaction.category_rule_id.is_not(None),
            Transaction.deleted_at.is_(None),
        )
        .group_by(Transaction.category_rule_id)
    )
    return {rule_id: count for rule_id, count in result}


def _read(rule: CategoryRule, catalog: CategoryCatalog, applied: int) -> RuleRead:
    category = catalog.by_id.get(rule.category_id)
    return RuleRead(
        id=rule.id,
        match_type=rule.match_type,
        pattern=rule.pattern,
        category_id=rule.category_id,
        category_label=category.label if category is not None else None,
        is_active=rule.is_active,
        created_at=rule.created_at,
        applied_count=applied,
    )


@router.get("", response_model=list[RuleRead])
async def list_rules(session: AsyncSession = Depends(get_tenant_session)) -> list[RuleRead]:
    """Todas as regras, ativas primeiro e a mais recente no topo."""
    catalog = await load_catalog(session)
    counts = await _applied_counts(session)
    rules = await session.scalars(
        select(CategoryRule)
        .where(CategoryRule.deleted_at.is_(None))
        .order_by(CategoryRule.is_active.desc(), CategoryRule.created_at.desc())
    )
    return [_read(rule, catalog, counts.get(rule.id, 0)) for rule in rules]


@router.patch("/{rule_id}", response_model=RuleUpdatedRead)
async def update_rule(
    rule_id: uuid.UUID,
    payload: RuleUpdateRequest,
    session: AsyncSession = Depends(get_tenant_session),
) -> RuleUpdatedRead:
    """Desativa ou reativa uma regra, desfazendo ou refazendo o que ela decide.

    **Desativar devolve à fila só o que esta regra decidiu** (`category_rule_id`), e
    não toca em linha que alguém redecidiu depois. As linhas voltam `PENDING`; quem
    as categoriza é a próxima execução do job — a tela dispara uma logo em seguida,
    num request à parte, depois de esta escrita ter sido confirmada no banco.

    **Reativar aplica de novo** ao que o LLM decidiu e ao que está na fila, como no
    dia em que foi criada.
    """
    rule = await session.get(CategoryRule, rule_id)
    if rule is None or rule.deleted_at is not None:
        # Sob RLS, regra de outro tenant não existe para esta sessão.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "regra não encontrada")

    catalog = await load_catalog(session)
    changed = 0
    if rule.is_active and not payload.is_active:
        rule.is_active = False
        await session.flush()
        changed = await reset_rule_decisions(session, rule_id=rule.id)
    elif not rule.is_active and payload.is_active:
        rule.is_active = True
        await session.flush()
        if rule.pattern is not None:
            changed = await apply_rules(
                session,
                rules=[
                    Rule(
                        id=rule.id,
                        match_type=rule.match_type,
                        pattern=rule.pattern,
                        category_id=rule.category_id,
                    )
                ],
                catalog=catalog,
                include_llm=True,
            )

    await session.refresh(rule)
    counts = await _applied_counts(session)
    read = _read(rule, catalog, counts.get(rule.id, 0))
    return RuleUpdatedRead(**read.model_dump(), transactions_changed=changed)
