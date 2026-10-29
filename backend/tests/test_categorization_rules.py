"""O passo de regras do job — evidência de conta própria e regras do titular.

Contra o banco e sob `app_tenant_session` (RLS ativo), pelo mesmo motivo de
`test_categorization_job.py`. O Ollama é um dublê que **conta as chamadas**: o que
estas regras decidem não pode gastar GPU, e um teste que só olhasse o resultado
gravado não pegaria um passo de regras que rodasse depois do LLM.
"""

import json
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.services.categorization.job import categorize_pending
from app.services.categorization.store import reset_rule_decisions
from app.services.ingestion import upsert_external_transactions

# Mesmo event loop das fixtures de sessão (ver pyproject.toml).
pytestmark = pytest.mark.asyncio(loop_scope="session")

ALIMENTACAO = uuid.UUID("00000000-0000-0000-0000-0000000001a1")
TRANSFERENCIAS = uuid.UUID("00000000-0000-0000-0000-0000000001b1")
CONTA_PROPRIA = uuid.UUID("00000000-0000-0000-0000-0000000001b2")
RECEITAS = uuid.UUID("00000000-0000-0000-0000-0000000001c1")
OUTRAS_RECEITAS = uuid.UUID("00000000-0000-0000-0000-0000000001c2")


@pytest.fixture
async def taxonomy_a(admin_session, tenants):
    """O mínimo para a precedência: uma despesa, uma receita e a conta própria."""
    tenant_a, _ = tenants
    rows = [
        (ALIMENTACAO, None, "Alimentação", "EXPENSE"),
        (TRANSFERENCIAS, None, "Transferências", "TRANSFER"),
        (CONTA_PROPRIA, TRANSFERENCIAS, "Transferência entre contas próprias", "TRANSFER"),
        (RECEITAS, None, "Receitas", "INCOME"),
        (OUTRAS_RECEITAS, RECEITAS, "Outras receitas", "INCOME"),
    ]
    for category_id, parent_id, name, kind in rows:
        await admin_session.execute(
            text(
                "INSERT INTO categories (id, tenant_id, parent_id, name, kind) "
                "VALUES (:id, :t, :p, :n, :k)"
            ),
            {
                "id": str(category_id),
                "t": str(tenant_a),
                "p": str(parent_id) if parent_id else None,
                "n": name,
                "k": kind,
            },
        )
    await admin_session.commit()


class CountingOllama:
    """Responde sempre a mesma categoria e conta quantas vezes foi chamado."""

    model = "fake-model"

    def __init__(self, category: str = "Receitas > Outras receitas", confidence: float = 0.95):
        self._answer = json.dumps({"category": category, "confidence": confidence})
        self.prompts: list[str] = []

    async def chat(self, *, messages, format_schema):
        self.prompts.append(messages[1]["content"])
        return self._answer


async def seed(app_tenant_session, tenant, account, specs) -> None:
    """Pela ingestão, como o sync grava. `specs`: (external_id, descrição, valor, flag)."""
    async with app_tenant_session(tenant) as session:
        await upsert_external_transactions(
            session,
            [
                {
                    "tenant_id": tenant,
                    "account_id": account,
                    "source": "PLUGGY",
                    "external_id": external_id,
                    "amount": amount,
                    "currency_code": "BRL",
                    "date": date(2026, 8, 5),
                    "description_raw": description,
                    "categorization_status": "PENDING",
                    "counterparty_is_holder": is_holder,
                }
                for external_id, description, amount, is_holder in specs
            ],
        )


async def row(app_tenant_session, tenant, external_id):
    async with app_tenant_session(tenant) as session:
        return (
            await session.execute(
                text(
                    "SELECT category_id, category_source, categorization_status, kind, "
                    "category_rule_id, category_confidence FROM transactions "
                    "WHERE external_id = :e"
                ),
                {"e": external_id},
            )
        ).one()


async def run(app_tenant_session, tenant, client):
    return await categorize_pending(
        tenant_id=tenant, client=client, session_scope=app_tenant_session
    )


# ---------------------------------------------------------------------------
# Evidência de conta própria
# ---------------------------------------------------------------------------


async def test_evidence_decides_own_transfer_without_calling_the_llm(
    tenants, account_a, taxonomy_a, app_tenant_session
) -> None:
    tenant_a, _ = tenants
    await seed(
        app_tenant_session,
        tenant_a,
        account_a,
        [("own", "PIX RECEBIDO FULANO DE TAL", Decimal("500.00"), True)],
    )
    client = CountingOllama()

    outcome = await run(app_tenant_session, tenant_a, client)

    assert outcome.status == "SUCCESS", outcome.error
    assert outcome.by_rule == 1
    assert client.prompts == []
    category_id, source, status, kind, rule_id, confidence = await row(
        app_tenant_session, tenant_a, "own"
    )
    assert (category_id, source, status, kind) == (CONTA_PROPRIA, "RULE", "CATEGORIZED", "TRANSFER")
    # NULL = a regra embutida, não uma de `category_rules`. Sem confiança: regra não
    # se autoavalia.
    assert rule_id is None
    assert confidence is None


async def test_evidence_overrides_the_llm_but_never_manual(
    tenants, account_a, taxonomy_a, admin_session, app_tenant_session
) -> None:
    """O Pix do próprio titular que o LLM mandou para Receitas antes de a evidência
    existir é corrigido; o que o titular respondeu à mão, não."""
    tenant_a, _ = tenants
    await seed(
        app_tenant_session,
        tenant_a,
        account_a,
        [
            ("llm", "PIX RECEBIDO FULANO", Decimal("300.00"), True),
            ("manual", "PIX RECEBIDO FULANO", Decimal("200.00"), True),
        ],
    )
    for external_id, source in (("llm", "LLM"), ("manual", "MANUAL")):
        await admin_session.execute(
            text(
                "UPDATE transactions SET category_id = :c, category_source = :s, "
                "categorization_status = 'CATEGORIZED', kind = 'INCOME' "
                "WHERE external_id = :e"
            ),
            {"c": str(OUTRAS_RECEITAS), "s": source, "e": external_id},
        )
    await admin_session.commit()

    await run(app_tenant_session, tenant_a, CountingOllama())

    assert (await row(app_tenant_session, tenant_a, "llm"))[:4] == (
        CONTA_PROPRIA,
        "RULE",
        "CATEGORIZED",
        "TRANSFER",
    )
    assert (await row(app_tenant_session, tenant_a, "manual"))[:4] == (
        OUTRAS_RECEITAS,
        "MANUAL",
        "CATEGORIZED",
        "INCOME",
    )


async def test_vanished_evidence_sends_the_row_back_through_the_llm(
    tenants, account_a, taxonomy_a, app_tenant_session
) -> None:
    """A origem corrigiu quem recebeu o Pix: a linha não pode ficar fora dos totais
    pela evidência que deixou de existir. Volta para a fila e o LLM decide na mesma
    execução."""
    tenant_a, _ = tenants
    spec = ("pix", "PIX RECEBIDO", Decimal("120.00"))
    await seed(app_tenant_session, tenant_a, account_a, [(*spec, True)])
    await run(app_tenant_session, tenant_a, CountingOllama())

    # Re-sync com a contraparte corrigida. O upsert preserva a decisão (já tem
    # `category_source`) e atualiza só o flag.
    await seed(app_tenant_session, tenant_a, account_a, [(*spec, False)])
    client = CountingOllama()
    outcome = await run(app_tenant_session, tenant_a, client)

    assert outcome.returned_to_queue == 1
    assert len(client.prompts) == 1
    assert "contraparte: não é o titular" in client.prompts[0]
    assert (await row(app_tenant_session, tenant_a, "pix"))[:4] == (
        OUTRAS_RECEITAS,
        "LLM",
        "CATEGORIZED",
        "INCOME",
    )


# ---------------------------------------------------------------------------
# Regras do titular
# ---------------------------------------------------------------------------


async def insert_rule(admin_session, tenant, pattern, category_id, priority=100) -> uuid.UUID:
    rule_id = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO category_rules (id, tenant_id, priority, match_type, pattern, "
            "category_id) VALUES (:id, :t, :p, 'CONTAINS', :pat, :c)"
        ),
        {
            "id": str(rule_id),
            "t": str(tenant),
            "p": priority,
            "pat": pattern,
            "c": str(category_id),
        },
    )
    await admin_session.commit()
    return rule_id


async def test_rule_decides_pending_rows_before_the_llm(
    tenants, account_a, taxonomy_a, admin_session, app_tenant_session
) -> None:
    tenant_a, _ = tenants
    rule_id = await insert_rule(admin_session, tenant_a, "PADARIA DO ZE", ALIMENTACAO)
    await seed(
        app_tenant_session,
        tenant_a,
        account_a,
        [
            ("padaria", "Padaria do Zé  SAO PAULO", Decimal("-12.00"), None),
            ("outra", "PIX RECEBIDO", Decimal("50.00"), None),
        ],
    )
    client = CountingOllama()

    outcome = await run(app_tenant_session, tenant_a, client)

    assert outcome.by_rule == 1
    # Só a linha sem regra chegou ao modelo.
    assert len(client.prompts) == 1
    assert "PIX RECEBIDO" in client.prompts[0]
    category_id, source, status, kind, applied_rule, _ = await row(
        app_tenant_session, tenant_a, "padaria"
    )
    assert (category_id, source, status, kind) == (ALIMENTACAO, "RULE", "CATEGORIZED", "EXPENSE")
    assert applied_rule == rule_id


async def test_rule_never_beats_the_evidence(
    tenants, account_a, taxonomy_a, admin_session, app_tenant_session
) -> None:
    tenant_a, _ = tenants
    await insert_rule(admin_session, tenant_a, "FULANO", OUTRAS_RECEITAS)
    await seed(
        app_tenant_session,
        tenant_a,
        account_a,
        [("own", "PIX RECEBIDO FULANO", Decimal("90.00"), True)],
    )

    await run(app_tenant_session, tenant_a, CountingOllama())

    assert (await row(app_tenant_session, tenant_a, "own"))[0] == CONTA_PROPRIA


async def test_reset_returns_only_what_that_rule_decided(
    tenants, account_a, taxonomy_a, admin_session, app_tenant_session
) -> None:
    """Desativar uma regra desfaz o que ela fez — e nada além disso."""
    tenant_a, _ = tenants
    padaria = await insert_rule(admin_session, tenant_a, "PADARIA", ALIMENTACAO)
    await insert_rule(admin_session, tenant_a, "SALARIO", OUTRAS_RECEITAS)
    await seed(
        app_tenant_session,
        tenant_a,
        account_a,
        [
            ("p", "PADARIA", Decimal("-5.00"), None),
            ("s", "SALARIO ACME", Decimal("5000.00"), None),
        ],
    )
    await run(app_tenant_session, tenant_a, CountingOllama())

    async with app_tenant_session(tenant_a) as session:
        affected = await reset_rule_decisions(session, rule_id=padaria)

    assert affected == 1
    assert (await row(app_tenant_session, tenant_a, "p"))[1:5] == (None, "PENDING", "EXPENSE", None)
    assert (await row(app_tenant_session, tenant_a, "s"))[1] == "RULE"
