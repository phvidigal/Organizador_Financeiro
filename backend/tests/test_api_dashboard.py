"""`GET /dashboard/summary` contra o banco, sob a conexão `app_user`.

A aritmética está coberta em `test_dashboard_pure.py`. O que só dá para verificar
aqui é a consulta: o filtro de período inclusivo nas duas pontas, a linha
excluída fora da soma, o filtro de conta e — o que mais importa — o RLS. Um
total que somasse a linha de outro tenant estaria errado sem erro nenhum.

As transações entram por `upsert_external_transactions` e são categorizadas por
`store.apply_decision`, os mesmos caminhos da aplicação (invariantes 1 e 1b).

Mesmo desenho de `test_api_review.py`: `httpx.ASGITransport`, sem lifespan e sem
servidor, com a sessão entrando por `dependency_overrides`.
"""

import uuid
from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import get_tenant_session, resolve_tenant_id
from app.main import create_app
from app.models.enums import CategorizationStatus
from app.services.categorization.decide import Decision
from app.services.categorization.store import apply_decision
from app.services.ingestion import upsert_external_transactions
from tests.conftest import TENANT_A, TENANT_B

# Mesmo event loop das fixtures de sessão (ver pyproject.toml).
pytestmark = pytest.mark.asyncio(loop_scope="session")

AUGUST = {"date_from": "2026-08-01", "date_to": "2026-08-31"}


@pytest.fixture
def api(app_tenant_session):
    def build(tenant_id=TENANT_A):
        app = create_app()

        async def override_session():
            async with app_tenant_session(tenant_id) as session:
                yield session

        app.dependency_overrides[get_tenant_session] = override_session
        app.dependency_overrides[resolve_tenant_id] = lambda: tenant_id
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")

    return build


@pytest.fixture
async def categories(admin_session: AsyncSession, tenants) -> dict[str, uuid.UUID]:
    """Uma árvore por `kind`, em dois níveis — `load_catalog` monta rótulos
    qualificados, e é deles que a quebra por categoria sai."""
    tenant_a, _ = tenants
    names = ("alim", "delivery", "rec", "salario", "transf", "fatura")
    ids = {name: uuid.uuid4() for name in names}
    await admin_session.execute(
        text(
            "INSERT INTO categories (id, tenant_id, parent_id, name, kind) VALUES "
            "(:alim,     :t, NULL,    'Alimentação',         'EXPENSE'), "
            "(:delivery, :t, :alim,   'Delivery',            'EXPENSE'), "
            "(:rec,      :t, NULL,    'Receitas',            'INCOME'), "
            "(:salario,  :t, :rec,    'Salário',             'INCOME'), "
            "(:transf,   :t, NULL,    'Transferências',      'TRANSFER'), "
            "(:fatura,   :t, :transf, 'Pagamento de cartão', 'TRANSFER')"
        ),
        {"t": str(tenant_a), **{k: str(v) for k, v in ids.items()}},
    )
    await admin_session.commit()
    return ids


async def create_account(admin_session: AsyncSession, tenant_id: uuid.UUID, name: str) -> uuid.UUID:
    account_id = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO accounts (id, tenant_id, type, subtype, name) "
            "VALUES (:id, :t, 'CREDIT', 'CREDIT_CARD', :name)"
        ),
        {"id": str(account_id), "t": str(tenant_id), "name": name},
    )
    await admin_session.commit()
    return account_id


async def seed(session_scope, tenant_id, account_id, rows) -> dict[str, uuid.UUID]:
    """Grava `(external_id, valor, data)` como PENDING e devolve os ids."""
    async with session_scope(tenant_id) as session:
        await upsert_external_transactions(
            session,
            [
                {
                    "tenant_id": tenant_id,
                    "account_id": account_id,
                    "source": "PLUGGY",
                    "external_id": external_id,
                    "amount": Decimal(amount),
                    "currency_code": "BRL",
                    "date": day,
                    "description_raw": external_id,
                    "categorization_status": "PENDING",
                }
                for external_id, amount, day in rows
            ],
        )
    async with session_scope(tenant_id) as session:
        result = await session.execute(
            text("SELECT external_id, id FROM transactions WHERE external_id = ANY(:e)"),
            {"e": [r[0] for r in rows]},
        )
        return dict(result.tuples().all())


async def decide(session_scope, transaction_id, category_id, kind, status="CATEGORIZED"):
    async with session_scope(TENANT_A) as session:
        await apply_decision(
            session,
            transaction_id=transaction_id,
            decision=Decision(
                status=CategorizationStatus(status),
                category_id=category_id,
                kind=kind,
                confidence=Decimal("0.950"),
            ),
        )


@pytest.fixture
async def august(app_tenant_session, admin_session, account_a, categories):
    """Um mês com um pouco de tudo: o que soma, o que fica fora e o que é provisório."""
    ids = await seed(
        app_tenant_session,
        TENANT_A,
        account_a,
        [
            ("salario", "5000.00", date(2026, 8, 5)),
            ("ifood", "-120.00", date(2026, 8, 1)),
            ("fatura", "-800.00", date(2026, 8, 10)),
            ("pix-pendente", "-50.00", date(2026, 8, 31)),
            ("ifood-duvida", "-30.00", date(2026, 8, 20)),
            ("julho", "-999.00", date(2026, 7, 31)),
            ("setembro", "-999.00", date(2026, 9, 1)),
            ("excluida", "-777.00", date(2026, 8, 15)),
        ],
    )
    await decide(app_tenant_session, ids["salario"], categories["salario"], "INCOME")
    await decide(app_tenant_session, ids["ifood"], categories["delivery"], "EXPENSE")
    await decide(app_tenant_session, ids["fatura"], categories["fatura"], "TRANSFER")
    await decide(
        app_tenant_session, ids["ifood-duvida"], categories["delivery"], "EXPENSE", "NEEDS_REVIEW"
    )
    # Não há caminho de aplicação que exclua: `deleted_at` hoje só significa "sumiu
    # na origem", e o sync nunca o escreve (ver CLAUDE.md, limitações da Fase 2).
    await admin_session.execute(
        text("UPDATE transactions SET deleted_at = now() WHERE id = :id"),
        {"id": str(ids["excluida"])},
    )
    await admin_session.commit()
    return ids


async def test_summary_sums_by_kind_and_declares_what_is_provisional(api, august):
    async with api() as client:
        response = await client.get("/dashboard/summary", params=AUGUST)

    assert response.status_code == 200, response.text
    body = response.json()

    # Dinheiro atravessa como string com duas casas.
    assert body["income"]["total"] == "5000.00"
    # -120 (decidido) -30 (em revisão) -50 (pendente, `kind` pelo sinal). Julho,
    # setembro e a excluída ficam fora.
    assert body["expense"]["total"] == "-200.00"
    assert body["expense"]["undecided_total"] == "-80.00"
    assert body["expense"]["undecided_count"] == 2
    # A fatura não é gasto.
    assert body["net"] == "4800.00"
    assert body["transfer"]["outflow"] == "-800.00"
    assert body["transfer"]["count"] == 1

    assert body["undecided"] == {"pending": 1, "needs_review": 1, "failed": 0}
    assert body["other_currency_count"] == 0
    # O mais recente fora do período, não dentro — é o que a tela vazia usa.
    assert body["latest_date"] == "2026-09-01"

    alimentacao, sem_categoria = body["expense_by_category"]
    assert (alimentacao["label"], alimentacao["total"]) == ("Alimentação", "-150.00")
    assert alimentacao["undecided_count"] == 1
    assert alimentacao["children"][0]["label"] == "Alimentação > Delivery"
    assert alimentacao["children"][0]["name"] == "Delivery"
    assert (sem_categoria["root_id"], sem_categoria["total"]) == (None, "-50.00")


async def test_summary_never_sees_another_tenant(api, august, app_tenant_session, admin_session):
    """O RLS é o que garante isto, não um WHERE: a consulta não filtra por tenant."""
    account_b = await create_account(admin_session, TENANT_B, "Cartão B")
    await seed(
        app_tenant_session,
        TENANT_B,
        account_b,
        [("outro-tenant", "-100000.00", date(2026, 8, 15))],
    )

    async with api() as client:
        body = (await client.get("/dashboard/summary", params=AUGUST)).json()

    assert body["expense"]["total"] == "-200.00"

    async with api(TENANT_B) as client:
        body_b = (await client.get("/dashboard/summary", params=AUGUST)).json()

    assert body_b["expense"]["total"] == "-100000.00"
    assert body_b["income"]["count"] == 0


async def test_summary_filters_by_account(api, august, app_tenant_session, admin_session):
    card = await create_account(admin_session, TENANT_A, "Cartão")
    await seed(app_tenant_session, TENANT_A, card, [("cartao", "-60.00", date(2026, 8, 3))])

    async with api() as client:
        everything = (await client.get("/dashboard/summary", params=AUGUST)).json()
        only_card = (
            await client.get("/dashboard/summary", params={**AUGUST, "account_id": str(card)})
        ).json()

    assert everything["expense"]["total"] == "-260.00"
    assert only_card["expense"]["total"] == "-60.00"
    assert only_card["income"]["total"] == "0.00"
    assert only_card["account_id"] == str(card)
    # O filtro de conta vale também para o "mais recente".
    assert only_card["latest_date"] == "2026-08-03"


async def test_summary_rejects_inverted_or_missing_period(api, tenants):
    async with api() as client:
        inverted = await client.get(
            "/dashboard/summary", params={"date_from": "2026-08-31", "date_to": "2026-08-01"}
        )
        missing = await client.get("/dashboard/summary", params={"date_from": "2026-08-01"})

    assert inverted.status_code == 422
    assert missing.status_code == 422


async def test_empty_period_is_zero_not_error(api, tenants):
    async with api() as client:
        response = await client.get("/dashboard/summary", params=AUGUST)

    assert response.status_code == 200
    body = response.json()
    assert body["net"] == "0.00"
    assert body["expense_by_category"] == []
    assert body["latest_date"] is None
