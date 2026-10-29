"""Catálogo, prompt e regra de decisão — tudo síncrono e sem banco.

Módulo **sem** `pytestmark = pytest.mark.asyncio`: nada aqui é assíncrono, e um
teste síncrono dentro de um módulo marcado quebra (ver CLAUDE.md).

É onde vive a maior parte da cobertura da Fase 3, de propósito: prompt e limiar são
o que mais vai mudar, e mudança em regra de classificação sem teste é regressão que
só aparece quando alguém confere uma categorização na tela — semanas depois.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.models.enums import CategorizationStatus
from app.services.categorization.catalog import (
    OWN_TRANSFER_LABEL,
    PIX_SENT_LABEL,
    CategoryRow,
    build_catalog,
)
from app.services.categorization.decide import (
    LOW_CONFIDENCE,
    OWN_TRANSFER_QUESTION,
    PIX_SENT_QUESTION,
    decide,
    question_for,
)
from app.services.categorization.errors import OllamaResponseError
from app.services.categorization.prompt import (
    Answer,
    TransactionForPrompt,
    build_messages,
    build_schema,
    describe_counterparty,
    format_amount,
    parse_response,
)
from app.services.categorization.rules import (
    Rule,
    first_match,
    normalize_pattern,
    rule_matches,
    suggest_pattern,
)

ALIMENTACAO = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
DELIVERY = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
SUPERMERCADO = uuid.UUID("00000000-0000-0000-0000-0000000000a3")
TRANSFERENCIAS = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
PAGAMENTO_CARTAO = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
TRANSPORTE = uuid.UUID("00000000-0000-0000-0000-0000000000c1")


def catalog():
    """Taxonomia mínima com o que importa: duas árvores EXPENSE e uma TRANSFER."""
    return build_catalog(
        [
            CategoryRow(ALIMENTACAO, None, "Alimentação", "EXPENSE", pluggy_category_id="10"),
            CategoryRow(DELIVERY, ALIMENTACAO, "Delivery", "EXPENSE", pluggy_category_id="11"),
            CategoryRow(SUPERMERCADO, ALIMENTACAO, "Supermercado", "EXPENSE"),
            CategoryRow(TRANSFERENCIAS, None, "Transferências", "TRANSFER"),
            CategoryRow(
                PAGAMENTO_CARTAO,
                TRANSFERENCIAS,
                "Pagamento de cartão",
                "TRANSFER",
                pluggy_category_id="20",
            ),
            CategoryRow(TRANSPORTE, None, "Transporte", "EXPENSE", pluggy_category_id="30"),
        ]
    )


# ---------------------------------------------------------------------------
# Catálogo e rótulos
# ---------------------------------------------------------------------------


def test_labels_are_qualified_by_parent() -> None:
    """Rótulo qualificado é o que torna o `enum` resolvível sem ambiguidade."""
    labels = catalog().labels

    assert "Alimentação" in labels
    assert "Alimentação > Delivery" in labels
    assert "Delivery" not in labels


def test_same_name_under_different_parents_stays_distinct() -> None:
    """O índice único é `(tenant_id, parent_id, name)`: nome repetido é legítimo.

    Com nomes crus no `enum`, "Manutenção" mapearia duas categorias e a resolução
    viraria chute. Qualificado, cada uma tem o seu rótulo.
    """
    moradia = uuid.uuid4()
    transporte = uuid.uuid4()
    cat = build_catalog(
        [
            CategoryRow(moradia, None, "Moradia", "EXPENSE"),
            CategoryRow(transporte, None, "Transporte", "EXPENSE"),
            CategoryRow(uuid.uuid4(), moradia, "Manutenção", "EXPENSE"),
            CategoryRow(uuid.uuid4(), transporte, "Manutenção", "EXPENSE"),
        ]
    )

    assert "Moradia > Manutenção" in cat.labels
    assert "Transporte > Manutenção" in cat.labels
    assert len(cat) == 4


def test_catalog_survives_a_parent_cycle() -> None:
    """Nada no schema impede `A.parent = B AND B.parent = A`.

    Sem o corte por ancestral já visto, seria recursão infinita no carregamento do
    catálogo — o job morreria antes da primeira chamada ao Ollama, com um
    `RecursionError` que não diz nada sobre a causa.
    """
    a, b = uuid.uuid4(), uuid.uuid4()
    cat = build_catalog(
        [
            CategoryRow(a, b, "A", "EXPENSE"),
            CategoryRow(b, a, "B", "EXPENSE"),
        ]
    )

    assert len(cat) == 2


def test_resolve_tolerates_case_and_whitespace() -> None:
    cat = catalog()

    assert cat.resolve("  alimentação > delivery ").id == DELIVERY
    assert cat.resolve("Categoria Inventada") is None


def test_pluggy_counterpart_uses_the_stored_map() -> None:
    cat = catalog()

    assert cat.pluggy_counterpart("11").id == DELIVERY
    # Categoria da Pluggy sem de/para: "sem contraparte", não "discordância".
    assert cat.pluggy_counterpart("999") is None
    assert cat.pluggy_counterpart(None) is None


# ---------------------------------------------------------------------------
# Schema e prompt
# ---------------------------------------------------------------------------


def test_schema_enumerates_every_label() -> None:
    """O `enum` é o que torna resposta fora da taxonomia impossível por construção."""
    cat = catalog()
    schema = build_schema(cat.labels)

    assert schema["properties"]["category"]["enum"] == cat.labels
    assert schema["required"] == ["category", "confidence"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["confidence"]["maximum"] == 1


def test_amount_keeps_the_sign_and_reads_in_pt_br() -> None:
    """O sinal é informação: é o que separa uma compra de um estorno."""
    assert format_amount(Decimal("-87.4")) == "-87,40 BRL"
    assert format_amount(Decimal("1500")) == "1.500,00 BRL"
    assert format_amount(Decimal("0")) == "0,00 BRL"


def test_messages_carry_the_transaction_and_the_aggregator_hint(transaction_factory) -> None:
    tx = transaction_factory(
        description_raw="PAGAMENTO FATURA CARTAO",
        merchant={"name": "Banco Exemplo"},
        pluggy_category_name="Credit card payment",
        account_type="CREDIT",
    )
    messages = build_messages(tx, catalog())

    assert [m["role"] for m in messages] == ["system", "user"]
    user = messages[1]["content"]
    assert "Alimentação > Delivery" in user
    assert "PAGAMENTO FATURA CARTAO" in user
    assert "Banco Exemplo" in user
    assert "Credit card payment" in user
    # O tipo da conta muda a leitura de "PAGAMENTO": no cartão é a fatura.
    assert "cartão de crédito" in user


def test_messages_omit_absent_fields(transaction_factory) -> None:
    """Linha em branco no prompt é ruído que o modelo tenta interpretar."""
    user = build_messages(transaction_factory(), catalog())[1]["content"]

    assert "estabelecimento:" not in user
    assert "palpite do agregador:" not in user


@pytest.fixture
def transaction_factory():
    from datetime import date

    from app.services.categorization.prompt import TransactionForPrompt

    def make(**overrides):
        defaults = {
            "id": uuid.uuid4(),
            "date": date(2026, 7, 14),
            "amount": Decimal("-87.40"),
            "description_raw": "COMPRA NO DEBITO",
        }
        return TransactionForPrompt(**{**defaults, **overrides})

    return make


# ---------------------------------------------------------------------------
# Leitura da resposta
# ---------------------------------------------------------------------------


def test_parse_reads_category_and_confidence() -> None:
    answer = parse_response('{"category": "Alimentação > Delivery", "confidence": 0.93}')

    assert answer.category == "Alimentação > Delivery"
    # Decimal e não float: a coluna é NUMERIC(4,3), e Decimal(0.93) não é 0.93.
    assert answer.confidence == Decimal("0.930")
    assert isinstance(answer.confidence, Decimal)


def test_parse_strips_a_reasoning_block() -> None:
    """`think: false` não é honrado por todo modelo, e o qwen3 é de raciocínio."""
    content = '<think>Vamos ver…\nÉ comida.</think>\n{"category": "Alimentação", "confidence": 1}'

    assert parse_response(content).category == "Alimentação"


def test_parse_keeps_the_category_when_confidence_is_unusable() -> None:
    """Confiança já é o sinal fraco: perder a categoria por causa dela seria pior."""
    assert parse_response('{"category": "Alimentação", "confidence": 7}').confidence is None
    assert parse_response('{"category": "Alimentação"}').confidence is None
    assert parse_response('{"category": "Alimentação", "confidence": "alta"}').confidence is None


def test_parse_rejects_what_cannot_be_used() -> None:
    with pytest.raises(OllamaResponseError):
        parse_response("desculpe, não consigo ajudar com isso")
    with pytest.raises(OllamaResponseError):
        parse_response('{"confidence": 0.9}')


# ---------------------------------------------------------------------------
# A regra de decisão
# ---------------------------------------------------------------------------


def test_high_confidence_and_agreement_is_categorized() -> None:
    decision = decide(
        Answer("Alimentação > Delivery", Decimal("0.95")),
        catalog=catalog(),
        pluggy_category_id="11",
    )

    assert decision.status == CategorizationStatus.CATEGORIZED
    assert decision.category_id == DELIVERY
    assert decision.kind == "EXPENSE"


def test_kind_is_inherited_from_the_category() -> None:
    """O ponto que o docs/fases-3-5.md marca como fácil de esquecer.

    Sem herdar o `kind`, um pagamento de fatura classificado como "Transferências"
    continua contando como gasto — e o dashboard mostra um número errado com cara
    de certo.
    """
    decision = decide(
        Answer("Transferências > Pagamento de cartão", Decimal("0.99")),
        catalog=catalog(),
        pluggy_category_id="20",
    )

    assert decision.kind == "TRANSFER"


def test_low_confidence_goes_to_review_but_keeps_the_guess() -> None:
    decision = decide(
        Answer("Alimentação > Delivery", LOW_CONFIDENCE - Decimal("0.001")),
        catalog=catalog(),
        pluggy_category_id="11",
    )

    assert decision.status == CategorizationStatus.NEEDS_REVIEW
    # A categoria fica gravada: a tela de revisão parte dela em vez de do nada.
    assert decision.category_id == DELIVERY


def test_disagreement_with_the_aggregator_goes_to_review() -> None:
    """O segundo sinal, e o motivo de ele existir.

    Confiança autodeclarada por LLM é mal calibrada — 0.95 sai com a mesma
    facilidade no acerto e no erro. Duas fontes independentes discordando é
    evidência melhor que a autoavaliação de uma.
    """
    decision = decide(
        Answer("Transporte", Decimal("0.99")),
        catalog=catalog(),
        pluggy_category_id="11",  # a Pluggy diz Delivery
    )

    assert decision.status == CategorizationStatus.NEEDS_REVIEW
    assert decision.category_id == TRANSPORTE
    assert "discorda" in (decision.reason or "")


def test_granularity_is_not_disagreement() -> None:
    """"Alimentação" contra "Alimentação > Delivery" é a mesma árvore.

    Mandar isso para revisão encheria a fila de acertos, que é a forma mais rápida
    de a tela de revisão deixar de ser usada.
    """
    decision = decide(
        Answer("Alimentação", Decimal("0.9")),
        catalog=catalog(),
        pluggy_category_id="11",
    )

    assert decision.status == CategorizationStatus.CATEGORIZED


def test_no_counterpart_means_no_comparison() -> None:
    """Categoria da Pluggy sem de/para não é discordância — é ausência de dado."""
    decision = decide(
        Answer("Transporte", Decimal("0.9")),
        catalog=catalog(),
        pluggy_category_id="999",
    )

    assert decision.status == CategorizationStatus.CATEGORIZED


def test_unknown_label_goes_to_review_not_failed() -> None:
    """Houve resposta, ela é que não serve. FAILED é para infraestrutura."""
    decision = decide(
        Answer("Gastos Diversos", Decimal("0.99")),
        catalog=catalog(),
        pluggy_category_id=None,
    )

    assert decision.status == CategorizationStatus.NEEDS_REVIEW
    assert decision.category_id is None
    # `kind` fica intocado: derivá-lo do nada apagaria uma marcação anterior.
    assert decision.kind is None


# ---------------------------------------------------------------------------
# Perguntas ao titular: Pix enviado e conta própria sem evidência
# ---------------------------------------------------------------------------

OUTROS = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
PIX_ENVIADO = uuid.UUID("00000000-0000-0000-0000-0000000000d2")
CONTA_PROPRIA = uuid.UUID("00000000-0000-0000-0000-0000000000b3")
OWN_LABEL = "Transferências > Transferência entre contas próprias"


def catalog_with_questions():
    cat = build_catalog(
        [
            CategoryRow(ALIMENTACAO, None, "Alimentação", "EXPENSE"),
            CategoryRow(TRANSFERENCIAS, None, "Transferências", "TRANSFER"),
            CategoryRow(
                CONTA_PROPRIA, TRANSFERENCIAS, "Transferência entre contas próprias", "TRANSFER"
            ),
            CategoryRow(OUTROS, None, "Outros", "EXPENSE"),
            CategoryRow(PIX_ENVIADO, OUTROS, "Pix enviado", "EXPENSE"),
        ]
    )
    # Os rótulos que o código conhece pelo nome existem neste catálogo.
    assert cat.own_transfer is not None and cat.own_transfer.label == OWN_TRANSFER_LABEL
    assert cat.pix_sent is not None and cat.pix_sent.label == PIX_SENT_LABEL
    return cat


def test_pix_sent_always_goes_to_review_and_counts_as_expense() -> None:
    """Escolher "Pix enviado" é o modelo dizendo que não sabe com o que foi gasto —
    mesmo com 0.95. O valor fica nos gastos enquanto o titular não responde."""
    decision = decide(
        Answer(category=PIX_SENT_LABEL, confidence=Decimal("0.95")),
        catalog=catalog_with_questions(),
        pluggy_category_id=None,
    )

    assert decision.status == CategorizationStatus.NEEDS_REVIEW
    assert decision.category_id == PIX_ENVIADO
    assert decision.kind == "EXPENSE"
    assert PIX_SENT_QUESTION in (decision.reason or "")


def test_own_transfer_without_evidence_is_a_question_not_a_transfer() -> None:
    """Sem evidência, "entre contas próprias" é palpite — e um palpite que tiraria o
    valor de todos os totais. Vai para a revisão **sem** mexer no `kind`."""
    decision = decide(
        Answer(category=OWN_LABEL, confidence=Decimal("0.95")),
        catalog=catalog_with_questions(),
        pluggy_category_id=None,
        counterparty_is_holder=None,
    )

    assert decision.status == CategorizationStatus.NEEDS_REVIEW
    assert decision.category_id == CONTA_PROPRIA
    assert decision.kind is None


def test_own_transfer_with_evidence_is_decided() -> None:
    decision = decide(
        Answer(category=OWN_LABEL, confidence=Decimal("0.95")),
        catalog=catalog_with_questions(),
        pluggy_category_id=None,
        counterparty_is_holder=True,
    )

    assert decision.status == CategorizationStatus.CATEGORIZED
    assert decision.kind == "TRANSFER"


def test_question_for_only_speaks_inside_the_review_queue() -> None:
    cat = catalog_with_questions()

    def ask(status, category_id, holder=None):
        return question_for(
            status=status, category_id=category_id, counterparty_is_holder=holder, catalog=cat
        )

    assert ask("NEEDS_REVIEW", PIX_ENVIADO) == PIX_SENT_QUESTION
    assert ask("NEEDS_REVIEW", CONTA_PROPRIA) == OWN_TRANSFER_QUESTION
    assert ask("NEEDS_REVIEW", CONTA_PROPRIA, holder=True) is None
    assert ask("CATEGORIZED", PIX_ENVIADO) is None
    assert ask("NEEDS_REVIEW", ALIMENTACAO) is None
    assert ask("NEEDS_REVIEW", None) is None


# ---------------------------------------------------------------------------
# Contraparte no prompt
# ---------------------------------------------------------------------------


def test_counterparty_line_uses_the_document_type_never_the_number() -> None:
    company = {"payer": {"documentNumber": {"type": "CNPJ"}}}
    person = {"receiver": {"documentNumber": {"type": "CPF"}}}

    assert describe_counterparty(Decimal("100"), None, company) == "empresa (CNPJ)"
    assert describe_counterparty(Decimal("-100"), False, person) == "outra pessoa física"
    assert describe_counterparty(Decimal("-100"), None, person) == "pessoa física"
    assert describe_counterparty(Decimal("-100"), True, person) == "o próprio titular"
    # Saída olha quem recebeu: o pagador de uma saída é sempre o titular.
    assert describe_counterparty(Decimal("-100"), None, company) is None
    assert describe_counterparty(Decimal("-100"), None, None) is None


def test_prompt_carries_the_counterparty_line() -> None:
    tx = TransactionForPrompt(
        id=uuid.uuid4(),
        date=date(2026, 8, 5),
        amount=Decimal("850.00"),
        description_raw="PIX RECEBIDO",
        counterparty="empresa (CNPJ)",
    )

    assert "contraparte: empresa (CNPJ)" in build_messages(tx, catalog())[1]["content"]


# ---------------------------------------------------------------------------
# Regras do titular
# ---------------------------------------------------------------------------


def rule(pattern: str, match_type: str = "CONTAINS") -> Rule:
    normalized = normalize_pattern(pattern)
    assert normalized is not None
    return Rule(id=uuid.uuid4(), match_type=match_type, pattern=normalized, category_id=DELIVERY)


def test_rule_matches_ignoring_case_accents_and_spacing() -> None:
    r = rule("joão  da silva")

    assert rule_matches(r, "PIX ENVIADO - JOAO DA SILVA 12/08")
    assert rule_matches(r, "Pix enviado João da   Silva")
    assert not rule_matches(r, "PIX ENVIADO MARIA DA SILVA")


def test_exact_rule_needs_the_whole_description() -> None:
    r = rule("netflix.com", match_type="EXACT")

    assert rule_matches(r, "NETFLIX.COM")
    assert not rule_matches(r, "NETFLIX.COM SAO PAULO")


def test_first_match_respects_the_given_order() -> None:
    specific, generic = rule("ifood mercado"), rule("ifood")

    assert first_match([specific, generic], "IFOOD MERCADO SP") is specific
    assert first_match([generic, specific], "IFOOD MERCADO SP") is generic
    assert first_match([specific], "UBER") is None


def test_pattern_too_short_is_refused() -> None:
    """ "PI" está em todo Pix: uma regra assim engoliria o extrato."""
    assert normalize_pattern(" pi ") is None
    assert normalize_pattern("   ") is None
    assert normalize_pattern("abc") == "ABC"


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("PIX ENVIADO 12/08 - FULANO DE TAL", "FULANO DE TAL"),
        ("Pix recebido João da Silva", "JOAO DA SILVA"),
        ("TRANSFERENCIA PIX PARA MARIA 0001 12345-6", "MARIA"),
        ("PADARIA DO ZE LTDA  SAO PAULO BR", "PADARIA DO ZE LTDA SAO PAULO"),
        ("PIX ENVIADO 123", None),
    ],
)
def test_suggested_pattern_keeps_what_repeats(description: str, expected: str | None) -> None:
    assert suggest_pattern(description) == expected
