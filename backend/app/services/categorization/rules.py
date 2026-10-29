"""Regras do titular — a camada da pipeline que vem antes do LLM.

É o laço de aprendizado que faltava: o titular responde uma vez na revisão,
marca "sempre categorizar assim", e o próximo lançamento com o mesmo trecho na
descrição nem chega ao Ollama. A tabela `category_rules` existia vazia desde a
Fase 1 com o formato fixado; isto é o que a lê.

Mesma divisão de `catalog.py`: a parte pura (normalizar, casar, sugerir o trecho)
é o que precisa de teste e não deveria exigir banco; a consulta e a aplicação
ficam no fim do módulo, e toda escrita passa por `store.apply_rule_decision`.

**Precedência**, do mais forte para o mais fraco: MANUAL, evidência de conta
própria, regra do titular, LLM. Uma regra nunca sobrescreve MANUAL nem a linha
cuja contraparte é o titular.
"""

import re
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.text import fold
from app.models.category_rule import CategoryRule
from app.models.enums import CategorySource, MatchType
from app.models.transaction import Transaction
from app.services.categorization.catalog import CategoryCatalog
from app.services.categorization.store import apply_rule_decision

# Abaixo disto o trecho casaria com quase tudo: "PI" está em todo Pix.
MIN_PATTERN_LENGTH = 3

# Os tipos que a pipeline sabe avaliar hoje. `REGEX` e `AMOUNT_RANGE` estão no
# schema desde a Fase 1, mas nenhuma tela os cria — e avaliar o que ninguém
# consegue criar seria código sem uso.
SUPPORTED_MATCH_TYPES = frozenset({MatchType.CONTAINS.value, MatchType.EXACT.value})

# Palavras que o extrato brasileiro põe em volta do que importa ("PIX ENVIADO -
# FULANO"). Só são cortadas nas **pontas**: "JOAO DA SILVA" precisa manter o "DA".
_GENERIC_TOKENS = frozenset(
    {
        "PIX", "ENVIADO", "ENVIADA", "RECEBIDO", "RECEBIDA", "TRANSF", "TRANSFERENCIA",
        "TED", "DOC", "COMPRA", "NO", "NA", "DEBITO", "CREDITO", "CARTAO", "PAGAMENTO",
        "PAG", "PGTO", "DE", "DA", "DO", "PARA", "EM", "VIA", "QR", "CODE", "QRCODE",
        "APP", "INTERNET", "BR", "BRA", "BRASIL",
    }
)  # fmt: skip
_SEPARATORS = re.compile(r"[\s\-/|:;,.()]+")
_HAS_DIGIT = re.compile(r"\d")


@dataclass(frozen=True)
class Rule:
    """O que a avaliação precisa saber de uma regra. Sem ORM, de propósito."""

    id: uuid.UUID
    match_type: str
    # Já normalizado por `fold` na criação: casar é comparar texto dobrado com
    # texto dobrado, sem normalizar o padrão a cada linha avaliada.
    pattern: str
    category_id: uuid.UUID


def normalize_pattern(pattern: str) -> str | None:
    """O trecho como ele é gravado e comparado. `None` se curto demais para casar
    com segurança."""
    folded = fold(pattern)
    if folded is None or len(folded) < MIN_PATTERN_LENGTH:
        return None
    return folded


def rule_matches(rule: Rule, description: str) -> bool:
    """A regra casa com esta descrição? Puro, e sem acento nem caixa no caminho."""
    folded = fold(description)
    if folded is None:
        return False
    if rule.match_type == MatchType.CONTAINS:
        return rule.pattern in folded
    if rule.match_type == MatchType.EXACT:
        return rule.pattern == folded
    return False


def first_match(rules: Sequence[Rule], description: str) -> Rule | None:
    """A primeira regra que casa, na ordem recebida — que já é a de prioridade."""
    return next((rule for rule in rules if rule_matches(rule, description)), None)


def suggest_pattern(description: str) -> str | None:
    """O trecho que a tela oferece para virar regra. O titular pode editar.

    Tira datas, valores e números de documento (todo token com dígito) e as palavras
    genéricas das pontas: de "PIX ENVIADO 12/08 - FULANO DE TAL" sobra "FULANO DE
    TAL", que é o que se repete no próximo Pix para a mesma pessoa.
    """
    folded = fold(description)
    if folded is None:
        return None
    tokens = [
        token for token in _SEPARATORS.split(folded) if token and not _HAS_DIGIT.search(token)
    ]
    while tokens and tokens[0] in _GENERIC_TOKENS:
        tokens.pop(0)
    while tokens and tokens[-1] in _GENERIC_TOKENS:
        tokens.pop()
    return normalize_pattern(" ".join(tokens))


# --- Consulta e aplicação ----------------------------------------------------


async def load_active_rules(session: AsyncSession) -> list[Rule]:
    """Regras ativas do tenant corrente, já na ordem de avaliação.

    O escopo vem do RLS. Menor `priority` primeiro; empate pela mais antiga, que é o
    que o modelo de `category_rules` documenta.
    """
    result = await session.execute(
        select(
            CategoryRule.id,
            CategoryRule.match_type,
            CategoryRule.pattern,
            CategoryRule.category_id,
        )
        .where(
            CategoryRule.is_active.is_(True),
            CategoryRule.deleted_at.is_(None),
            CategoryRule.match_type.in_(SUPPORTED_MATCH_TYPES),
            CategoryRule.pattern.is_not(None),
        )
        .order_by(CategoryRule.priority, CategoryRule.created_at)
    )
    return [
        Rule(id=row.id, match_type=row.match_type, pattern=row.pattern, category_id=row.category_id)
        for row in result
    ]


async def apply_rules(
    session: AsyncSession,
    *,
    rules: Sequence[Rule],
    catalog: CategoryCatalog,
    include_llm: bool,
    now: datetime | None = None,
) -> int:
    """Aplica as regras às linhas que elas podem decidir. Devolve quantas mudaram.

    `include_llm=False` é o passo do job: só o que ainda não tem decisão (fonte
    NULL — PENDING ou FAILED). `include_llm=True` é a regra recém-criada, que vale
    também para o que o LLM já tinha decidido: é para isso que o titular a criou.

    Nunca MANUAL, nunca a linha cuja contraparte é o titular — a evidência ganha.
    A avaliação é em Python, e não em SQL, porque `fold` tira acento e o Postgres
    não tem `unaccent` instalado; o volume de um titular (centenas de linhas por
    mês) não pesa.
    """
    if not rules:
        return 0

    sources = [Transaction.category_source.is_(None)]
    if include_llm:
        sources.append(Transaction.category_source == CategorySource.LLM.value)

    result = await session.execute(
        select(Transaction.id, Transaction.description_raw).where(
            Transaction.deleted_at.is_(None),
            Transaction.counterparty_is_holder.is_not(True),
            or_(*sources),
        )
    )

    matched: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for row in result:
        rule = first_match(rules, row.description_raw)
        if rule is not None:
            matched[rule.id].append(row.id)

    applied = 0
    for rule in rules:
        ids = matched.get(rule.id)
        if not ids:
            continue
        category = catalog.by_id.get(rule.category_id)
        if category is None:
            # Regra apontando para categoria desativada: não ressuscita a categoria
            # pela porta dos fundos. As linhas seguem para o LLM.
            continue
        applied += await apply_rule_decision(
            session, transaction_ids=ids, category=category, rule_id=rule.id, now=now
        )
    return applied
