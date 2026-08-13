"""O job de categorização, ponta a ponta contra o banco.

Roda sob `app_tenant_session` — conexão `app_user`, RLS ativo —, e não com a sessão
de owner: o job roda fora de request em produção, e testar com o owner esconderia
uma policy faltando ou um `set_tenant_scope` esquecido, cujo modo de falha é a
consulta devolver zero linhas em silêncio.

O cliente do Ollama aqui é um dublê, não `MockTransport`: o alvo destes testes é a
orquestração — o que é gravado, o que fica na fila, o que sobrevive a uma queda.
O protocolo HTTP é coberto em `test_ollama_client.py`.
"""

import json
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.models.enums import CategorySource
from app.services.categorization.errors import (
    OllamaResponseError,
    OllamaUnavailableError,
)
from app.services.categorization.job import categorize_pending
from app.services.categorization.store import reset_categorization
from app.services.ingestion import upsert_external_transactions

# Mesmo event loop das fixtures de sessão (ver pyproject.toml).
pytestmark = pytest.mark.asyncio(loop_scope="session")


ALIMENTACAO = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
DELIVERY = uuid.UUID("00000000-0000-0000-0000-0000000000f2")
TRANSFERENCIAS = uuid.UUID("00000000-0000-0000-0000-0000000000f3")
PAGAMENTO_CARTAO = uuid.UUID("00000000-0000-0000-0000-0000000000f4")


@pytest.fixture
async def categories_a(admin_session, tenants):
    """Taxonomia do tenant A.

    A migration só semeia categorias para o tenant padrão, então um tenant de teste
    nasce sem nenhuma — e sem taxonomia não há `enum`, que é o coração da Fase 3.

    "Pagamento de cartão" é TRANSFER de propósito: é o par que prova a herança do
    `kind`, que é o detalhe mais fácil de perder.
    """
    tenant_a, _ = tenants
    rows = [
        (ALIMENTACAO, None, "Alimentação", "EXPENSE", "10"),
        (DELIVERY, ALIMENTACAO, "Delivery", "EXPENSE", "11"),
        (TRANSFERENCIAS, None, "Transferências", "TRANSFER", None),
        (PAGAMENTO_CARTAO, TRANSFERENCIAS, "Pagamento de cartão", "TRANSFER", "20"),
    ]
    for category_id, parent_id, name, kind, pluggy_id in rows:
        await admin_session.execute(
            text(
                "INSERT INTO categories (id, tenant_id, parent_id, name, kind, "
                "pluggy_category_id) VALUES (:id, :t, :p, :n, :k, :pg)"
            ),
            {
                "id": str(category_id),
                "t": str(tenant_a),
                "p": str(parent_id) if parent_id else None,
                "n": name,
                "k": kind,
                "pg": pluggy_id,
            },
        )
    await admin_session.commit()
    return rows


class FakeOllama:
    """Dublê do cliente. `answers` é consultado por trecho da descrição."""

    model = "fake-model"

    def __init__(self, answers: dict[str, tuple[str, float]], fail_from: int | None = None):
        self._answers = answers
        self._fail_from = fail_from
        self.calls = 0

    async def chat(self, *, messages, format_schema):
        self.calls += 1
        if self._fail_from is not None and self.calls >= self._fail_from:
            raise OllamaUnavailableError("ollama fora do ar")

        prompt = messages[1]["content"]
        for needle, (category, confidence) in self._answers.items():
            if needle in prompt:
                return json.dumps({"category": category, "confidence": confidence})
        raise AssertionError(f"prompt inesperado: {prompt}")


async def seed_transactions(app_tenant_session, tenant_a, account_a, specs) -> None:
    """Grava pela ingestão, nunca por INSERT próprio (invariante 1 do CLAUDE.md)."""
    async with app_tenant_session(tenant_a) as session:
        await upsert_external_transactions(
            session,
            [
                {
                    "tenant_id": tenant_a,
                    "account_id": account_a,
                    "source": "PLUGGY",
                    "external_id": external_id,
                    "amount": amount,
                    "currency_code": "BRL",
                    "date": date(2026, 8, 1),
                    "description_raw": description,
                    "pluggy_category_id": pluggy_category_id,
                    "pluggy_category_name": None,
                    "categorization_status": "PENDING",
                }
                for external_id, description, amount, pluggy_category_id in specs
            ],
        )


async def fetch(app_tenant_session, tenant_a, external_id: str):
    async with app_tenant_session(tenant_a) as session:
        return (
            await session.execute(
                text(
                    "SELECT category_id, category_source, categorization_status, "
                    "category_confidence, categorized_at, kind "
                    "FROM transactions WHERE external_id = :e"
                ),
                {"e": external_id},
            )
        ).one()


# ---------------------------------------------------------------------------
# Caminho feliz
# ---------------------------------------------------------------------------


async def test_writes_all_six_columns_at_once(
    tenants, account_a, categories_a, app_tenant_session
) -> None:
    """As seis colunas andam juntas — é a razão de `store.py` existir."""
    tenant_a, _ = tenants
    await seed_transactions(
        app_tenant_session, tenant_a, account_a, [("t1", "IFD*IFOOD", Decimal("-53.20"), "11")]
    )

    client = FakeOllama({"IFD*IFOOD": ("Alimentação > Delivery", 0.94)})
    outcome = await categorize_pending(
        tenant_id=tenant_a, client=client, session_scope=app_tenant_session
    )

    assert outcome.status == "SUCCESS"
    assert (outcome.processed, outcome.categorized) == (1, 1)

    row = await fetch(app_tenant_session, tenant_a, "t1")
    assert row.category_id == DELIVERY
    assert row.category_source == CategorySource.LLM
    assert row.categorization_status == "CATEGORIZED"
    assert row.category_confidence == Decimal("0.940")
    assert row.categorized_at is not None
    assert row.kind == "EXPENSE"


async def test_kind_is_inherited_and_flips_expense_to_transfer(
    tenants, account_a, categories_a, app_tenant_session
) -> None:
    """O teste que protege o dashboard da Fase 4.

    A ingestão derivou EXPENSE do sinal negativo, porque nenhuma origem de dado diz
    sozinha que um lançamento é transferência. Categorizar é o momento em que isso
    se resolve — e sem herdar o `kind` da categoria, o pagamento da fatura continua
    contando como gasto e o mês fecha com o dobro do valor.
    """
    tenant_a, _ = tenants
    await seed_transactions(
        app_tenant_session,
        tenant_a,
        account_a,
        [("t2", "PAGAMENTO FATURA CARTAO", Decimal("-1200.00"), "20")],
    )

    assert (await fetch(app_tenant_session, tenant_a, "t2")).kind == "EXPENSE"

    client = FakeOllama({"PAGAMENTO FATURA": ("Transferências > Pagamento de cartão", 0.97)})
    await categorize_pending(
        tenant_id=tenant_a, client=client, session_scope=app_tenant_session
    )

    row = await fetch(app_tenant_session, tenant_a, "t2")
    assert row.category_id == PAGAMENTO_CARTAO
    assert row.kind == "TRANSFER"


async def test_disagreement_with_the_aggregator_lands_in_needs_review(
    tenants, account_a, categories_a, app_tenant_session
) -> None:
    tenant_a, _ = tenants
    await seed_transactions(
        app_tenant_session, tenant_a, account_a, [("t3", "PIX ENVIADO", Decimal("-90.00"), "11")]
    )

    # A Pluggy diz Delivery (id 11); o LLM diz Transferências. Raízes diferentes.
    client = FakeOllama({"PIX ENVIADO": ("Transferências", 0.99)})
    outcome = await categorize_pending(
        tenant_id=tenant_a, client=client, session_scope=app_tenant_session
    )

    assert outcome.needs_review == 1
    row = await fetch(app_tenant_session, tenant_a, "t3")
    assert row.categorization_status == "NEEDS_REVIEW"
    # Continua sendo decisão do LLM: é preservada no próximo re-sync.
    assert row.category_source == CategorySource.LLM
    assert row.category_id == TRANSFERENCIAS


# ---------------------------------------------------------------------------
# A fila
# ---------------------------------------------------------------------------


async def test_queue_skips_what_is_not_pending(
    tenants, account_a, categories_a, app_tenant_session
) -> None:
    """Só PENDING entra. É o que torna o job barato de repetir."""
    tenant_a, _ = tenants
    await seed_transactions(
        app_tenant_session,
        tenant_a,
        account_a,
        [
            ("t4", "IFD*IFOOD", Decimal("-30.00"), "11"),
            ("t5", "JA CATEGORIZADA", Decimal("-10.00"), None),
            ("t6", "EXCLUIDA NA ORIGEM", Decimal("-20.00"), None),
        ],
    )
    async with app_tenant_session(tenant_a) as session:
        await session.execute(
            text(
                "UPDATE transactions SET categorization_status = 'CATEGORIZED', "
                "category_source = 'MANUAL' WHERE external_id = 't5'"
            )
        )
        await session.execute(
            text("UPDATE transactions SET deleted_at = now() WHERE external_id = 't6'")
        )

    client = FakeOllama({"IFD*IFOOD": ("Alimentação > Delivery", 0.9)})
    outcome = await categorize_pending(
        tenant_id=tenant_a, client=client, session_scope=app_tenant_session
    )

    assert outcome.processed == 1
    assert client.calls == 1


async def test_limit_stops_the_run(
    tenants, account_a, categories_a, app_tenant_session
) -> None:
    """`limit` existe para conferir o prompt em dez linhas antes do backlog inteiro."""
    tenant_a, _ = tenants
    await seed_transactions(
        app_tenant_session,
        tenant_a,
        account_a,
        [(f"L{i}", "IFD*IFOOD", Decimal("-10.00"), None) for i in range(5)],
    )

    client = FakeOllama({"IFD*IFOOD": ("Alimentação > Delivery", 0.9)})
    outcome = await categorize_pending(
        tenant_id=tenant_a, client=client, session_scope=app_tenant_session, limit=2
    )

    assert outcome.processed == 2
    assert client.calls == 2


async def test_tenant_without_categories_does_nothing(
    tenants, account_a, app_tenant_session
) -> None:
    """Sem taxonomia não há `enum`, e um `enum` vazio não restringe nada."""
    tenant_a, _ = tenants
    await seed_transactions(
        app_tenant_session, tenant_a, account_a, [("t7", "QUALQUER", Decimal("-1.00"), None)]
    )

    client = FakeOllama({})
    outcome = await categorize_pending(
        tenant_id=tenant_a, client=client, session_scope=app_tenant_session
    )

    assert outcome.status == "PARTIAL"
    assert client.calls == 0
    assert (await fetch(app_tenant_session, tenant_a, "t7")).categorization_status == "PENDING"


# ---------------------------------------------------------------------------
# Falha
# ---------------------------------------------------------------------------


async def test_run_is_resumable_after_the_ollama_goes_down(
    tenants, account_a, categories_a, app_tenant_session
) -> None:
    """Uma queda no meio do backlog não pode perder o que já foi feito.

    Sai de graça porque cada transação é gravada na própria unidade de trabalho e a
    fila é definida por `categorization_status`. Um lote único numa transação de
    banco só desfaria horas de GPU no primeiro erro.
    """
    tenant_a, _ = tenants
    await seed_transactions(
        app_tenant_session,
        tenant_a,
        account_a,
        [(f"R{i}", "IFD*IFOOD", Decimal("-10.00"), None) for i in range(6)],
    )

    client = FakeOllama({"IFD*IFOOD": ("Alimentação > Delivery", 0.9)}, fail_from=3)
    outcome = await categorize_pending(
        tenant_id=tenant_a, client=client, session_scope=app_tenant_session
    )

    assert outcome.status == "FAILED"
    assert outcome.categorized == 2

    async with app_tenant_session(tenant_a) as session:
        counts = dict(
            (
                await session.execute(
                    text(
                        "SELECT categorization_status, count(*) FROM transactions "
                        "GROUP BY 1"
                    )
                )
            ).all()
        )

    assert counts["CATEGORIZED"] == 2
    # As demais continuam na fila — nenhuma foi marcada como falha.
    assert counts["PENDING"] == 4
    assert "FAILED" not in counts

    # E o backlog é retomado sem reprocessar o que já foi feito.
    resumed = FakeOllama({"IFD*IFOOD": ("Alimentação > Delivery", 0.9)})
    again = await categorize_pending(
        tenant_id=tenant_a, client=resumed, session_scope=app_tenant_session
    )
    assert again.processed == 4


async def test_ollama_down_from_the_start_marks_nothing_as_failed(
    tenants, account_a, categories_a, app_tenant_session
) -> None:
    """Marcar centenas de linhas como FAILED exigiria um reset manual para recuperar.

    Falhar seguro aqui é deixá-las PENDING: a próxima execução tenta de novo sozinha.
    """
    tenant_a, _ = tenants
    await seed_transactions(
        app_tenant_session,
        tenant_a,
        account_a,
        [(f"D{i}", "IFD*IFOOD", Decimal("-10.00"), None) for i in range(4)],
    )

    client = FakeOllama({}, fail_from=1)
    outcome = await categorize_pending(
        tenant_id=tenant_a, client=client, session_scope=app_tenant_session
    )

    assert outcome.status == "FAILED"
    assert outcome.processed == 0
    assert outcome.error is not None
    # Três tentativas consecutivas e desiste — não varre o backlog inteiro.
    assert client.calls == 3

    async with app_tenant_session(tenant_a) as session:
        pending = await session.scalar(
            text(
                "SELECT count(*) FROM transactions WHERE categorization_status = 'PENDING'"
            )
        )
    assert pending == 4


async def test_unusable_answer_goes_to_review_and_the_run_continues(
    tenants, account_a, categories_a, app_tenant_session, monkeypatch
) -> None:
    """Houve resposta, ela é que não serve: problema de uma linha, não da execução."""
    tenant_a, _ = tenants
    await seed_transactions(
        app_tenant_session,
        tenant_a,
        account_a,
        [
            ("U1", "DESCRICAO RUIM", Decimal("-10.00"), None),
            ("U2", "IFD*IFOOD", Decimal("-20.00"), None),
        ],
    )

    class Flaky(FakeOllama):
        async def chat(self, *, messages, format_schema):
            if "DESCRICAO RUIM" in messages[1]["content"]:
                raise OllamaResponseError("modelo devolveu prosa")
            return await super().chat(messages=messages, format_schema=format_schema)

    client = Flaky({"IFD*IFOOD": ("Alimentação > Delivery", 0.9)})
    outcome = await categorize_pending(
        tenant_id=tenant_a, client=client, session_scope=app_tenant_session
    )

    assert outcome.status == "SUCCESS"
    assert (outcome.categorized, outcome.needs_review) == (1, 1)

    row = await fetch(app_tenant_session, tenant_a, "U1")
    assert row.categorization_status == "NEEDS_REVIEW"
    assert row.category_id is None


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


async def test_reset_requeues_llm_rows_and_never_touches_manual(
    tenants, account_a, categories_a, app_tenant_session
) -> None:
    """O que torna a iteração de prompt barata — sem custar a correção do usuário."""
    tenant_a, _ = tenants
    await seed_transactions(
        app_tenant_session,
        tenant_a,
        account_a,
        [
            ("X1", "IFD*IFOOD", Decimal("-10.00"), None),
            ("X2", "CORRIGIDA A MAO", Decimal("-20.00"), None),
        ],
    )

    client = FakeOllama(
        {
            "IFD*IFOOD": ("Alimentação > Delivery", 0.9),
            "CORRIGIDA A MAO": ("Alimentação", 0.9),
        }
    )
    await categorize_pending(
        tenant_id=tenant_a, client=client, session_scope=app_tenant_session
    )

    async with app_tenant_session(tenant_a) as session:
        await session.execute(
            text(
                "UPDATE transactions SET category_source = 'MANUAL', kind = 'TRANSFER' "
                "WHERE external_id = 'X2'"
            )
        )

    async with app_tenant_session(tenant_a) as session:
        affected = await reset_categorization(session, source=CategorySource.LLM)

    assert affected == 1

    reset_row = await fetch(app_tenant_session, tenant_a, "X1")
    assert reset_row.categorization_status == "PENDING"
    assert reset_row.category_id is None
    assert reset_row.category_source is None
    assert reset_row.categorized_at is None
    # `kind` volta a ser derivado do sinal, como estava antes da categorização.
    assert reset_row.kind == "EXPENSE"

    kept = await fetch(app_tenant_session, tenant_a, "X2")
    assert kept.category_source == "MANUAL"
    assert kept.kind == "TRANSFER"


async def test_reset_of_manual_is_refused(tenants, app_tenant_session) -> None:
    """A correção do usuário é a régua para medir o LLM. Nada no sistema a apaga."""
    tenant_a, _ = tenants
    async with app_tenant_session(tenant_a) as session:
        with pytest.raises(ValueError):
            await reset_categorization(session, source=CategorySource.MANUAL)
