"""Regras nascidas de uma resposta, e o extrato que as mostra.

`PATCH /transactions/{id}` com `rule_pattern` é o laço de aprendizado: o titular
responde uma vez e o mesmo trecho deixa de ser perguntado. Três coisas precisam
continuar verdade com a regra no meio:

* a revisão continua registrada antes da correção (a régua da Fase 4);
* a regra nunca toca no que o titular já respondeu;
* um pedido recusado não deixa nada gravado pela metade.

Mesmo desenho de `test_api_review.py`: `httpx.ASGITransport`, sessão por
`dependency_overrides`, conexão `app_user` com RLS.
"""

import uuid
from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from app.core.tenancy import get_tenant_session, resolve_tenant_id
from app.main import create_app
from app.services.ingestion import upsert_external_transactions
from tests.conftest import TENANT_A

# Mesmo event loop das fixtures de sessão (ver pyproject.toml).
pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture
def api(app_tenant_session):
    def build():
        app = create_app()

        async def override_session():
            async with app_tenant_session(TENANT_A) as session:
                yield session

        app.dependency_overrides[get_tenant_session] = override_session
        app.dependency_overrides[resolve_tenant_id] = lambda: TENANT_A
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")

    return build


@pytest.fixture
async def taxonomy(admin_session, tenants) -> dict[str, uuid.UUID]:
    tenant_a, _ = tenants
    ids = {name: uuid.uuid4() for name in ("alim", "transf", "propria", "outros", "pix")}
    await admin_session.execute(
        text(
            "INSERT INTO categories (id, tenant_id, parent_id, name, kind) VALUES "
            "(:alim, :t, NULL, 'Alimentação', 'EXPENSE'), "
            "(:transf, :t, NULL, 'Transferências', 'TRANSFER'), "
            "(:propria, :t, :transf, 'Transferência entre contas próprias', 'TRANSFER'), "
            "(:outros, :t, NULL, 'Outros', 'EXPENSE'), "
            "(:pix, :t, :outros, 'Pix enviado', 'EXPENSE')"
        ),
        {"t": str(tenant_a), **{k: str(v) for k, v in ids.items()}},
    )
    await admin_session.commit()
    return ids


async def seed(app_tenant_session, tenant, account, specs) -> dict[str, uuid.UUID]:
    """`specs`: (external_id, descrição, valor). Devolve external_id → id."""
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
                }
                for external_id, description, amount in specs
            ],
        )
        rows = await session.execute(text("SELECT external_id, id FROM transactions"))
        return dict(rows.all())


async def decide_as(admin_session, external_id, category_id, source, status="CATEGORIZED"):
    """Simula uma decisão anterior, de quem quer que seja."""
    await admin_session.execute(
        text(
            "UPDATE transactions SET category_id = :c, category_source = :s, "
            "categorization_status = :st, kind = 'EXPENSE' WHERE external_id = :e"
        ),
        {"c": str(category_id), "s": source, "st": status, "e": external_id},
    )
    await admin_session.commit()


async def state(app_tenant_session, external_id):
    async with app_tenant_session(TENANT_A) as session:
        return (
            await session.execute(
                text(
                    "SELECT category_id, category_source, categorization_status, "
                    "category_rule_id FROM transactions WHERE external_id = :e"
                ),
                {"e": external_id},
            )
        ).one()


async def count(app_tenant_session, table: str) -> int:
    async with app_tenant_session(TENANT_A) as session:
        return await session.scalar(text(f"SELECT count(*) FROM {table}"))


# ---------------------------------------------------------------------------
# PATCH com regra
# ---------------------------------------------------------------------------


async def test_rule_from_an_answer_applies_to_llm_and_queue_but_not_manual(
    api, tenants, account_a, taxonomy, admin_session, app_tenant_session
) -> None:
    tenant_a, _ = tenants
    ids = await seed(
        app_tenant_session,
        tenant_a,
        account_a,
        [
            ("respondida", "PIX ENVIADO 01/08 JOAO DA SILVA", Decimal("-80.00")),
            ("llm", "PIX ENVIADO JOÃO DA SILVA", Decimal("-80.00")),
            ("fila", "Pix enviado joao da silva", Decimal("-40.00")),
            ("manual", "PIX ENVIADO JOAO DA SILVA", Decimal("-10.00")),
            ("outra", "PIX ENVIADO MARIA", Decimal("-10.00")),
        ],
    )
    await decide_as(admin_session, "llm", taxonomy["pix"], "LLM", "NEEDS_REVIEW")
    await decide_as(admin_session, "manual", taxonomy["outros"], "MANUAL")

    async with api() as client:
        response = await client.patch(
            f"/transactions/{ids['respondida']}",
            json={"category_id": str(taxonomy["alim"]), "rule_pattern": "joão da silva"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["category_source"] == "MANUAL"
    assert body["rule_id"] is not None
    assert body["rule_applied"] == 2

    rule_id = uuid.UUID(body["rule_id"])
    for external_id in ("llm", "fila"):
        assert tuple(await state(app_tenant_session, external_id)) == (
            taxonomy["alim"],
            "RULE",
            "CATEGORIZED",
            rule_id,
        )
    assert (await state(app_tenant_session, "manual"))[1] == "MANUAL"
    assert (await state(app_tenant_session, "outra"))[1] is None
    # A régua da Fase 4 continua sendo escrita.
    assert await count(app_tenant_session, "categorization_reviews") == 1


@pytest.mark.parametrize(
    ("payload_extra", "message"),
    [
        ({"rule_pattern": "jo"}, "pelo menos"),
        ({"rule_pattern": "MARIA"}, "não aparece"),
        ({"rule_pattern": "JOAO", "kind": "TRANSFER"}, "natureza"),
    ],
)
async def test_refused_rule_writes_nothing(
    api, tenants, account_a, taxonomy, app_tenant_session, payload_extra, message
) -> None:
    """Recusa antes de qualquer escrita: nem revisão, nem correção, nem regra."""
    tenant_a, _ = tenants
    ids = await seed(
        app_tenant_session, tenant_a, account_a, [("t", "PIX ENVIADO JOAO", Decimal("-5.00"))]
    )

    async with api() as client:
        response = await client.patch(
            f"/transactions/{ids['t']}",
            json={"category_id": str(taxonomy["alim"]), **payload_extra},
        )

    assert response.status_code == 400
    assert message in response.json()["detail"]
    assert await count(app_tenant_session, "categorization_reviews") == 0
    assert await count(app_tenant_session, "category_rules") == 0
    assert (await state(app_tenant_session, "t"))[1] is None


# ---------------------------------------------------------------------------
# /rules
# ---------------------------------------------------------------------------


async def test_deactivating_returns_its_rows_and_reactivating_reapplies(
    api, tenants, account_a, taxonomy, app_tenant_session
) -> None:
    tenant_a, _ = tenants
    ids = await seed(
        app_tenant_session,
        tenant_a,
        account_a,
        [
            ("a", "PADARIA DO ZE", Decimal("-5.00")),
            ("b", "PADARIA DO ZE", Decimal("-7.00")),
        ],
    )
    async with api() as client:
        created = await client.patch(
            f"/transactions/{ids['a']}",
            json={"category_id": str(taxonomy["alim"]), "rule_pattern": "padaria do ze"},
        )
        rule_id = created.json()["rule_id"]

        listed = await client.get("/rules")
        assert listed.status_code == 200
        [rule] = listed.json()
        assert rule["pattern"] == "PADARIA DO ZE"
        assert rule["category_label"] == "Alimentação"
        assert rule["applied_count"] == 1

        off = await client.patch(f"/rules/{rule_id}", json={"is_active": False})
        assert off.status_code == 200, off.text
        assert off.json()["transactions_changed"] == 1
        assert tuple(await state(app_tenant_session, "b"))[1:] == (None, "PENDING", None)
        # A linha que o titular respondeu não era da regra.
        assert (await state(app_tenant_session, "a"))[1] == "MANUAL"

        on = await client.patch(f"/rules/{rule_id}", json={"is_active": True})
        assert on.json()["transactions_changed"] == 1
        assert (await state(app_tenant_session, "b"))[1] == "RULE"


async def test_unknown_rule_is_404(api, tenants) -> None:
    async with api() as client:
        response = await client.patch(f"/rules/{uuid.uuid4()}", json={"is_active": False})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /transactions: busca, conta própria e campos calculados
# ---------------------------------------------------------------------------


async def test_search_treats_percent_and_underscore_literally(
    api, tenants, account_a, taxonomy, app_tenant_session
) -> None:
    tenant_a, _ = tenants
    await seed(
        app_tenant_session,
        tenant_a,
        account_a,
        [
            ("desconto", "CUPOM 50% OFF", Decimal("-1.00")),
            ("cinquenta", "CUPOM 500 OFF", Decimal("-1.00")),
            ("sub", "PAG_SEGURO", Decimal("-1.00")),
            ("subx", "PAGXSEGURO", Decimal("-1.00")),
        ],
    )

    async with api() as client:
        percent = (await client.get("/transactions", params={"q": "50%"})).json()
        underscore = (await client.get("/transactions", params={"q": "g_s"})).json()

    assert [t["external_id"] for t in percent["items"]] == ["desconto"]
    assert [t["external_id"] for t in underscore["items"]] == ["sub"]


async def test_hiding_own_transfers_keeps_uncategorized_rows(
    api, tenants, account_a, taxonomy, admin_session, app_tenant_session
) -> None:
    tenant_a, _ = tenants
    await seed(
        app_tenant_session,
        tenant_a,
        account_a,
        [("propria", "PIX", Decimal("-100.00")), ("sem", "PIX", Decimal("-1.00"))],
    )
    await decide_as(admin_session, "propria", taxonomy["propria"], "RULE")

    async with api() as client:
        hidden = (await client.get("/transactions", params={"hide_own_transfers": "true"})).json()
        shown = (await client.get("/transactions")).json()

    assert [t["external_id"] for t in hidden["items"]] == ["sem"]
    assert hidden["total"] == 1
    assert shown["total"] == 2


async def test_listing_carries_the_question_and_the_suggested_pattern(
    api, tenants, account_a, taxonomy, admin_session, app_tenant_session
) -> None:
    tenant_a, _ = tenants
    await seed(
        app_tenant_session,
        tenant_a,
        account_a,
        [("pix", "PIX ENVIADO 12/08 FULANO DE TAL", Decimal("-30.00"))],
    )
    await decide_as(admin_session, "pix", taxonomy["pix"], "LLM", "NEEDS_REVIEW")

    async with api() as client:
        [item] = (await client.get("/transactions")).json()["items"]

    assert item["review_question"] == "Com o que foi gasto este Pix?"
    assert item["suggested_rule_pattern"] == "FULANO DE TAL"
