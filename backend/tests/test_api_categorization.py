"""Endpoints da Fase 3, exercitados pela app inteira.

Mesmo desenho de `test_api_connections.py`: `httpx.ASGITransport`, sem lifespan e
sem servidor, com a sessão e o cliente do Ollama entrando por
`dependency_overrides`. O `runner` é neutralizado — aqui o alvo é a resposta do
endpoint, não a task de background, que tem o seu próprio módulo.
"""

import uuid
from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from app.api.deps import get_ollama_client
from app.core.tenancy import get_tenant_session, resolve_tenant_id
from app.main import create_app
from app.services.categorization import runner
from app.services.ingestion import upsert_external_transactions
from tests.conftest import TENANT_A

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture
def api(app_tenant_session, monkeypatch):
    scheduled: list[dict] = []

    def fake_schedule(**kwargs) -> bool:
        scheduled.append(kwargs)
        return True

    monkeypatch.setattr(runner, "schedule_categorization", fake_schedule)

    def build(*, running: bool = False):
        app = create_app()

        async def override_session():
            async with app_tenant_session(TENANT_A) as session:
                yield session

        app.dependency_overrides[get_tenant_session] = override_session
        app.dependency_overrides[resolve_tenant_id] = lambda: TENANT_A
        app.dependency_overrides[get_ollama_client] = lambda: object()

        if running:
            monkeypatch.setattr(runner, "schedule_categorization", lambda **kwargs: False)
            monkeypatch.setattr(runner, "is_running", lambda _: True)

        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    build.scheduled = scheduled
    return build


@pytest.fixture
async def pending_transactions(app_tenant_session, tenants, account_a):
    tenant_a, _ = tenants
    async with app_tenant_session(tenant_a) as session:
        await upsert_external_transactions(
            session,
            [
                {
                    "tenant_id": tenant_a,
                    "account_id": account_a,
                    "source": "PLUGGY",
                    "external_id": f"api-{i}",
                    "amount": Decimal("-10.00"),
                    "currency_code": "BRL",
                    "date": date(2026, 8, 1),
                    "description_raw": "PADARIA",
                    "categorization_status": "PENDING",
                }
                for i in range(3)
            ],
        )


async def test_run_returns_202_and_the_queue(api, pending_transactions) -> None:
    """202 e nunca bloqueia: o backlog inicial leva de dez a vinte minutos."""
    async with api() as client:
        response = await client.post("/categorization/run")

    assert response.status_code == 202
    body = response.json()
    assert body["queue"]["pending"] == 3
    assert body["tenant_id"] == str(TENANT_A)
    assert api.scheduled[0]["tenant_id"] == TENANT_A


async def test_run_forwards_the_limit(api, pending_transactions) -> None:
    async with api() as client:
        await client.post("/categorization/run", params={"limit": 10})

    assert api.scheduled[0]["limit"] == 10


async def test_second_run_while_one_is_going_is_409(api, pending_transactions) -> None:
    """O lock é por tenant: dois disparos categorizariam as mesmas linhas duas vezes."""
    async with api(running=True) as client:
        response = await client.post("/categorization/run")

    assert response.status_code == 409
    assert response.json()["running"] is True


async def test_status_counts_the_queue_by_status(
    api, app_tenant_session, tenants, pending_transactions
) -> None:
    tenant_a, _ = tenants
    async with app_tenant_session(tenant_a) as session:
        await session.execute(
            text(
                "UPDATE transactions SET categorization_status = 'NEEDS_REVIEW', "
                "category_source = 'LLM' WHERE external_id = 'api-0'"
            )
        )

    async with api() as client:
        body = (await client.get("/categorization/status")).json()

    assert body["queue"] == {
        "pending": 2,
        "categorized": 0,
        "needs_review": 1,
        "failed": 0,
    }


async def test_reset_requeues_llm_rows(
    api, app_tenant_session, tenants, pending_transactions
) -> None:
    tenant_a, _ = tenants
    async with app_tenant_session(tenant_a) as session:
        await session.execute(
            text(
                "UPDATE transactions SET categorization_status = 'CATEGORIZED', "
                "category_source = 'LLM', kind = 'TRANSFER' WHERE external_id <> 'api-0'"
            )
        )

    async with api() as client:
        body = (await client.post("/categorization/reset")).json()

    assert body == {"source": "LLM", "transactions_reset": 2}

    async with app_tenant_session(tenant_a) as session:
        pending = await session.scalar(
            text("SELECT count(*) FROM transactions WHERE categorization_status = 'PENDING'")
        )
    assert pending == 3


async def test_reset_of_manual_is_refused(api) -> None:
    """A correção do usuário é a régua para medir o LLM. Nenhum endpoint a apaga."""
    async with api() as client:
        response = await client.post("/categorization/reset", params={"source": "MANUAL"})

    assert response.status_code == 400


async def test_status_is_scoped_to_the_tenant(api, admin_session, tenants) -> None:
    """Sob RLS, a fila de outro tenant simplesmente não existe daqui.

    A contagem é a única coisa que a Fase 4 vai mostrar antes de qualquer login: se
    ela vazasse, vazaria o volume de transações de outro titular.
    """
    _, tenant_b = tenants
    account_b = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO accounts (id, tenant_id, type, subtype, name) "
            "VALUES (:id, :t, 'BANK', 'CHECKING_ACCOUNT', 'Conta do B')"
        ),
        {"id": str(account_b), "t": str(tenant_b)},
    )
    await admin_session.execute(
        text(
            "INSERT INTO transactions (tenant_id, account_id, source, external_id, "
            "amount, kind, date, description_raw, categorization_status) "
            "VALUES (:t, :a, 'PLUGGY', 'do-b', -5.00, 'EXPENSE', '2026-08-01', "
            "'PADARIA DO B', 'PENDING')"
        ),
        {"t": str(tenant_b), "a": str(account_b)},
    )
    await admin_session.commit()

    async with api() as client:
        body = (await client.get("/categorization/status")).json()

    assert body["queue"]["pending"] == 0
    assert uuid.UUID(body["tenant_id"]) == TENANT_A
