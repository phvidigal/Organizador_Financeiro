"""A regra que separa `CATEGORIZED` de `NEEDS_REVIEW`. Pura.

O `docs/fases-3-5.md` registra o problema em uma frase: **confiança autodeclarada
por LLM é mal calibrada** — modelos dizem 0.95 com a mesma facilidade com que
acertam e erram. Um limiar sozinho produziria uma fila de revisão que não
corresponde aos erros reais.

Por isso o segundo sinal: a **concordância com a categorização nativa da Pluggy**.
Duas fontes independentes discordando é evidência melhor que a autoavaliação de
uma. A Fase 2 gravou `pluggy_category_id` em toda transação e
`sync_category_map` ligou a taxonomia deles à nossa exatamente para isto.

O que **não** se faz aqui: misturar os dois sinais num número só e gravar o
resultado em `category_confidence`. Essa coluna guarda o número cru do modelo,
porque é ele que precisa ser comparado às correções `MANUAL` da Fase 4 para que a
calibração real seja medida algum dia. A concordância continua derivável a
qualquer momento por join entre `transactions.pluggy_category_id` e
`categories.pluggy_category_id` — nenhuma coluna nova é necessária.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

from app.models.enums import CategorizationStatus
from app.services.categorization.catalog import CategoryCatalog, CategoryEntry
from app.services.categorization.prompt import Answer

# Abaixo disto, revisão humana. Constante de módulo e não variável de ambiente, no
# molde de `MIN_SYNC_INTERVAL`: é uma decisão de produto que precisa de revisão de
# código para mudar, não de um `.env` que ninguém relê.
#
# 0.70 é um ponto de partida declaradamente arbitrário. O número honesto só sai
# depois da Fase 4, comparando estas previsões com as correções do usuário.
LOW_CONFIDENCE = Decimal("0.70")

# As perguntas que só o titular responde. Escolher uma destas categorias **é** o
# modelo dizendo "não tenho como saber" — por isso a linha vai para a revisão com
# qualquer confiança, e o texto é o que a tela mostra (`question_for`).
PIX_SENT_QUESTION = "Com o que foi gasto este Pix?"
OWN_TRANSFER_QUESTION = "Esta transferência foi entre contas suas?"


@dataclass(frozen=True)
class Decision:
    """O que será gravado. `kind=None` significa "não mexa no que já está lá"."""

    status: CategorizationStatus
    category_id: uuid.UUID | None = None
    kind: str | None = None
    confidence: Decimal | None = None
    # Só para log e para o resumo da execução; não vai para o banco.
    reason: str | None = None


def _question(
    entry: CategoryEntry | None,
    *,
    catalog: CategoryCatalog,
    counterparty_is_holder: bool | None,
) -> str | None:
    """A pergunta que a escolha desta categoria representa, se for uma.

    Fonte única das duas regras: `decide` as usa para mandar a linha à revisão, e
    `question_for` para a tela dizer o que está sendo perguntado.
    """
    if entry is None:
        return None
    pix_sent = catalog.pix_sent
    if pix_sent is not None and entry.id == pix_sent.id:
        return PIX_SENT_QUESTION
    own = catalog.own_transfer
    if own is not None and entry.id == own.id and counterparty_is_holder is not True:
        # Sem evidência, "entre contas próprias" é palpite — e um palpite que tira o
        # valor de todos os totais. Só a evidência decide sozinha.
        return OWN_TRANSFER_QUESTION
    return None


def question_for(
    *,
    status: str,
    category_id: uuid.UUID | None,
    counterparty_is_holder: bool | None,
    catalog: CategoryCatalog,
) -> str | None:
    """O que a revisão pergunta ao titular sobre esta linha. `None` fora da fila."""
    if status != CategorizationStatus.NEEDS_REVIEW or category_id is None:
        return None
    return _question(
        catalog.by_id.get(category_id),
        catalog=catalog,
        counterparty_is_holder=counterparty_is_holder,
    )


def decide(
    answer: Answer,
    *,
    catalog: CategoryCatalog,
    pluggy_category_id: str | None,
    counterparty_is_holder: bool | None = None,
) -> Decision:
    """Traduz a resposta do modelo na decisão de gravação."""
    entry = catalog.resolve(answer.category)

    if entry is None:
        # O `enum` do schema deveria tornar isto impossível; se aconteceu, o modelo
        # ignorou o `format`. NEEDS_REVIEW e não FAILED: houve resposta, ela é que
        # não serve — FAILED é para infraestrutura.
        return Decision(
            status=CategorizationStatus.NEEDS_REVIEW,
            confidence=answer.confidence,
            reason=f"categoria fora da taxonomia: {answer.category!r}",
        )

    question = _question(entry, catalog=catalog, counterparty_is_holder=counterparty_is_holder)
    if question is not None:
        own = catalog.own_transfer
        return Decision(
            status=CategorizationStatus.NEEDS_REVIEW,
            category_id=entry.id,
            # Conta própria sem evidência mantém o `kind` do sinal (`None` = não
            # mexa): até o titular confirmar, o valor continua nos totais — o erro
            # oposto esconderia receita ou gasto real (invariante 4). O Pix enviado
            # herda EXPENSE normalmente.
            kind=None if own is not None and entry.id == own.id else entry.kind,
            confidence=answer.confidence,
            reason=f"pergunta ao titular: {question}",
        )

    if answer.confidence is None or answer.confidence < LOW_CONFIDENCE:
        return Decision(
            status=CategorizationStatus.NEEDS_REVIEW,
            category_id=entry.id,
            kind=entry.kind,
            confidence=answer.confidence,
            reason=f"confiança {answer.confidence} abaixo de {LOW_CONFIDENCE}",
        )

    counterpart = catalog.pluggy_counterpart(pluggy_category_id)
    if counterpart is not None and counterpart.root_id != entry.root_id:
        # Comparação por **raiz**, não por folha: o LLM dizer "Alimentação" onde a
        # Pluggy diz "Alimentação > Restaurantes e bares" é diferença de
        # granularidade, e mandar isso para revisão encheria a fila de acertos.
        # Discordar de raiz — "Transporte" contra "Compras" — é discordância real.
        return Decision(
            status=CategorizationStatus.NEEDS_REVIEW,
            category_id=entry.id,
            kind=entry.kind,
            confidence=answer.confidence,
            reason=f"discorda do agregador, que aponta {counterpart.label!r}",
        )

    return Decision(
        status=CategorizationStatus.CATEGORIZED,
        category_id=entry.id,
        # Herdar o `kind` da categoria é o ponto que o `docs/fases-3-5.md` marca
        # como fácil de esquecer: sem isto, um Pix classificado como
        # "Transferências" continua contando como gasto no dashboard.
        kind=entry.kind,
        confidence=answer.confidence,
    )
