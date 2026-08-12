"""Endpoints da Fase 2, exercitados pela app inteira.

`httpx.ASGITransport` em vez de um servidor de verdade: o lifespan **não** roda,
que é exatamente o desejado — `assert_rls_enforced` apontaria para o banco de
produção, não para o de teste.

A sessão e o cliente da Pluggy entram por `dependency_overrides`. O resto é a
aplicação real: roteamento, validação de schema, serialização.
"""

import uuid
from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_pluggy_client
from app.core.tenancy import get_tenant_session, resolve_tenant_id
from app.main import create_app
from app.services.pluggy import runner
from tests.conftest import TENANT_A
from tests.pluggy_fixtures import ITEM_ID, make_client

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture
def api(app_tenant_session, monkeypatch):
    """Cliente HTTP da aplicação, com banco de teste e Pluggy falsa.

    `schedule_sync` é neutralizado por padrão: quase todo teste quer verificar a
    resposta do endpoint, não esperar uma task de background terminar. Quem quer o
    sync de verdade chama `sync_connection` (ver `test_pluggy_sync.py`).
    """
    monkeypatch.setattr(runner, "schedule_sync", lambda **kwargs: True)

    def build(routes=None, *, running: bool = False):
        app = create_app()
        pluggy = make_client(routes)

        async def override_session():
            async with app_tenant_session(TENANT_A) as session:
                yield session

        app.dependency_overrides[get_tenant_session] = override_session
        app.dependency_overrides[resolve_tenant_id] = lambda: TENANT_A
        app.dependency_overrides[get_pluggy_client] = lambda: pluggy

        if running:
            monkeypatch.setattr(runner, "is_running", lambda _: True)

        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )

    return build


@pytest.fixture
async def connection_a(admin_session: AsyncSession, tenants) -> uuid.UUID:
    tenant_a, _ = tenants
    connection_id = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO bank_connections (id, tenant_id, pluggy_item_id, status) "
            "VALUES (:id, :t, :i, 'UPDATED')"
        ),
        {"id": str(connection_id), "t": str(tenant_a), "i": str(ITEM_ID)},
    )
    await admin_session.commit()
    return connection_id


# --- Adoção de item ----------------------------------------------------------


async def test_adopting_an_item_creates_the_connection(api, tenants) -> None:
    async with api() as client:
        response = await client.post("/connections", json={"item_id": str(ITEM_ID)})

    assert response.status_code == 201
    body = response.json()
    assert body["pluggy_item_id"] == str(ITEM_ID)
    # Nasce preenchida: o endpoint consulta a Pluggy antes de gravar, para a linha
    # não começar com placeholders que a primeira sync corrigiria.
    assert body["connector_name"] == "Banco de Teste"
    assert body["status"] == "UPDATED"
    assert body["next_auto_sync_at"] is not None


async def test_adopting_the_same_item_twice_is_idempotent(api, tenants) -> None:
    """Repetir uma ação que já produziu o efeito desejado não é conflito — e a
    constraint existe para que não vire uma segunda conexão sincronizando as
    mesmas contas."""
    async with api() as client:
        first = await client.post("/connections", json={"item_id": str(ITEM_ID)})
        second = await client.post("/connections", json={"item_id": str(ITEM_ID)})
        listed = await client.get("/connections")

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert len(listed.json()) == 1


async def test_unknown_item_is_rejected_before_touching_the_database(api, tenants) -> None:
    async with api(routes={}) as client:
        response = await client.post("/connections", json={"item_id": str(uuid.uuid4())})
        listed = await client.get("/connections")

    assert response.status_code == 404
    assert listed.json() == []


async def test_malformed_item_id_is_a_validation_error(api, tenants) -> None:
    """A coluna é `PgUUID NOT NULL`. Validar no schema dá 422 com mensagem clara,
    em vez de um 500 vindo do driver."""
    async with api() as client:
        response = await client.post("/connections", json={"item_id": "não-é-uuid"})

    assert response.status_code == 422


# --- Disparo de sync ---------------------------------------------------------


async def test_sync_returns_202_immediately(api, connection_a) -> None:
    """Nunca bloqueia: ler todas as páginas leva de segundos a minutos, e a
    resposta já traz o estado atual para a tela pintar "sincronizando…"."""
    async with api() as client:
        response = await client.post(f"/connections/{connection_a}/sync")

    assert response.status_code == 202
    body = response.json()
    assert body["connection_id"] == str(connection_a)
    assert body["throttled"] is False


async def test_second_sync_in_a_row_is_throttled(api, connection_a) -> None:
    """Dois F5 seguidos não podem virar dois syncs. O `last_synced_at` é gravado
    no início justamente para reservar."""
    async with api() as client:
        first = await client.post(f"/connections/{connection_a}/sync")
        second = await client.post(f"/connections/{connection_a}/sync")

    assert first.status_code == 202
    assert second.status_code == 409
    body = second.json()
    assert body["throttled"] is True
    assert 0 < body["retry_after_seconds"] <= 600


async def test_force_bypasses_the_throttle(api, connection_a) -> None:
    async with api() as client:
        await client.post(f"/connections/{connection_a}/sync")
        forced = await client.post(f"/connections/{connection_a}/sync?force=true")

    assert forced.status_code == 202
    assert forced.json()["throttled"] is False


async def test_sync_status_reports_a_running_task(api, connection_a) -> None:
    """`running` vem do registro em memória do runner; o resto vem do banco."""
    async with api(running=True) as client:
        response = await client.get(f"/connections/{connection_a}/sync")

    assert response.status_code == 200
    assert response.json()["running"] is True


async def test_sync_of_an_unknown_connection_is_404(api, tenants) -> None:
    async with api() as client:
        response = await client.post(f"/connections/{uuid.uuid4()}/sync")

    assert response.status_code == 404


# --- Exclusão ----------------------------------------------------------------


async def test_delete_is_a_soft_delete(api, connection_a, app_tenant_session) -> None:
    """A FK de `accounts` é `ON DELETE CASCADE`: apagar de verdade levaria meses de
    extrato junto, sem volta, por causa de um clique."""
    async with api() as client:
        deleted = await client.delete(f"/connections/{connection_a}")
        listed = await client.get("/connections")
        fetched = await client.get(f"/connections/{connection_a}")

    assert deleted.status_code == 204
    assert listed.json() == []
    assert fetched.status_code == 404

    async with app_tenant_session(TENANT_A) as session:
        row = (
            await session.execute(
                text("SELECT deleted_at FROM bank_connections WHERE id = :id"),
                {"id": str(connection_a)},
            )
        ).one()
    assert row.deleted_at is not None


async def test_readopting_a_deleted_connection_revives_it(api, connection_a) -> None:
    """Sem isso, readotar esbarraria na constraint com um erro que o usuário não
    teria como interpretar."""
    async with api() as client:
        await client.delete(f"/connections/{connection_a}")
        readopted = await client.post("/connections", json={"item_id": str(ITEM_ID)})
        listed = await client.get("/connections")

    assert readopted.status_code == 200
    assert readopted.json()["id"] == str(connection_a)
    assert len(listed.json()) == 1


# --- Contas e transações -----------------------------------------------------


@pytest.fixture
async def synced_data(admin_session: AsyncSession, tenants, connection_a) -> uuid.UUID:
    """Uma conta com três transações, montada direto no banco."""
    tenant_a, _ = tenants
    account_id = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO accounts (id, tenant_id, bank_connection_id, type, subtype, name, "
            "                      balance, currency_code) "
            "VALUES (:id, :t, :c, 'BANK', 'CHECKING_ACCOUNT', 'Conta Corrente', 777.40, 'BRL')"
        ),
        {"id": str(account_id), "t": str(tenant_a), "c": str(connection_a)},
    )
    # Tipos Python de verdade nos binds: o asyncpg não aceita `'2026-08-01'` como
    # string onde a coluna é `date` (o psycopg aceitaria), e o mesmo vale para
    # `NUMERIC` — que, aliás, nunca deve receber float.
    for n, (amount, kind, day) in enumerate(
        [(Decimal("-12.34"), "EXPENSE", 1), (Decimal("1500.00"), "INCOME", 2),
         (Decimal("-99.90"), "EXPENSE", 3)]
    ):
        await admin_session.execute(
            text(
                "INSERT INTO transactions (tenant_id, account_id, source, external_id, amount, "
                "  kind, date, description_raw, categorization_status, pluggy_category_name) "
                "VALUES (:t, :a, 'PLUGGY', :e, :amt, :k, :d, :desc, 'PENDING', 'Groceries')"
            ),
            {
                "t": str(tenant_a),
                "a": str(account_id),
                "e": f"api-tx-{n}",
                "amt": amount,
                "k": kind,
                "d": date(2026, 8, day),
                "desc": f"LANCAMENTO {n}",
            },
        )
    await admin_session.commit()
    return account_id


async def test_accounts_are_listed_with_the_balance_as_a_string(api, synced_data) -> None:
    """Dinheiro atravessa a API como string: `JSON.parse` do navegador
    transformaria número em double, e 0,10 voltaria a não ser representável."""
    async with api() as client:
        response = await client.get("/accounts")

    assert response.status_code == 200
    (account,) = response.json()
    assert account["balance"] == "777.40"
    assert isinstance(account["balance"], str)


async def test_transactions_are_paginated_newest_first(api, synced_data) -> None:
    async with api() as client:
        response = await client.get("/transactions?limit=2")

    body = response.json()
    assert body["total"] == 3
    assert body["limit"] == 2
    assert len(body["items"]) == 2
    assert [t["date"] for t in body["items"]] == ["2026-08-03", "2026-08-02"]


async def test_transaction_amounts_keep_two_decimal_places(api, synced_data) -> None:
    async with api() as client:
        body = (await client.get("/transactions")).json()

    amounts = {t["amount"] for t in body["items"]}
    # "1500.00" e não "1500": o cliente não deveria precisar adivinhar a escala.
    assert amounts == {"-12.34", "1500.00", "-99.90"}


async def test_transactions_can_be_filtered(api, synced_data) -> None:
    async with api() as client:
        by_kind = (await client.get("/transactions?kind=INCOME")).json()
        by_date = (await client.get("/transactions?date_from=2026-08-02")).json()
        by_account = (await client.get(f"/transactions?account_id={uuid.uuid4()}")).json()

    assert by_kind["total"] == 1
    assert by_date["total"] == 2
    assert by_account["total"] == 0


async def test_transactions_expose_the_pluggy_guess_without_adopting_it(api, synced_data) -> None:
    """A régua da Fase 3: o palpite está gravado, mas a transação segue na fila."""
    async with api() as client:
        body = (await client.get("/transactions")).json()

    for item in body["items"]:
        assert item["pluggy_category_name"] == "Groceries"
        assert item["categorization_status"] == "PENDING"
        assert item["category_source"] is None
        # `raw_payload` não sai na listagem.
        assert "raw_payload" not in item


async def test_limit_above_the_ceiling_is_rejected(api, synced_data) -> None:
    async with api() as client:
        response = await client.get("/transactions?limit=500")

    assert response.status_code == 422


# --- De/para de categorias ---------------------------------------------------


async def test_category_map_can_be_rerun_on_demand(
    api, admin_session, tenants, app_tenant_session
) -> None:
    """Editar `PLUGGY_TO_LOCAL` e ver o efeito sem esperar 24h pela próxima
    coleta da Pluggy."""
    tenant_a, _ = tenants
    await admin_session.execute(
        text(
            "INSERT INTO categories (id, tenant_id, name, kind) "
            "VALUES (:id, :t, 'Alimentação', 'EXPENSE')"
        ),
        {"id": str(uuid.uuid4()), "t": str(tenant_a)},
    )
    await admin_session.commit()

    async with api() as client:
        response = await client.post("/categories/pluggy-map")

    assert response.status_code == 200
    body = response.json()
    assert body["mapped"] == 1

    async with app_tenant_session(TENANT_A) as session:
        gravado = await session.scalar(
            text("SELECT pluggy_category_id FROM categories WHERE name = 'Alimentação'")
        )
    assert gravado == "05000000"

    # As demais entradas do de/para viram conflito porque este tenant de teste não
    # tem a taxonomia semeada — é o relatório fazendo o que deve.
    assert any("não existe na taxonomia" in c for c in body["conflicts"])


# --- Credenciais ausentes ----------------------------------------------------


async def test_missing_credentials_are_a_503_not_a_crash(app_tenant_session, monkeypatch) -> None:
    """Sem isso, um `docker compose up` com o `.env` vazio derrubaria a API
    inteira — inclusive o `/health` que o frontend usa para mostrar o que falta.

    O monkeypatch é em `app.api.deps.get_settings` e não em
    `dependency_overrides`: `get_pluggy_client` chama a função direto, não via
    `Depends`, então o override do FastAPI não a alcançaria.
    """
    from app.api import deps
    from app.core.config import Settings

    # `pluggy_*` explícitos como kwarg: `_env_file=None` desliga só o arquivo, e as
    # variáveis do container continuariam preenchendo os campos.
    sem_credencial = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@db:5432/finance",
        pluggy_client_id=None,
        pluggy_client_secret=None,
    )
    monkeypatch.setattr(deps, "get_settings", lambda: sem_credencial)

    app = create_app()

    async def override_session():
        async with app_tenant_session(TENANT_A) as session:
            yield session

    app.dependency_overrides[get_tenant_session] = override_session
    app.dependency_overrides[resolve_tenant_id] = lambda: TENANT_A

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/connections", json={"item_id": str(ITEM_ID)})

    assert response.status_code == 503
    assert "PLUGGY_CLIENT_ID" in response.json()["detail"]


# --- Isolamento por tenant ---------------------------------------------------


async def test_a_connection_of_another_tenant_is_invisible(
    api, admin_session, tenants
) -> None:
    """Sob RLS, "não existe" e "é de outro tenant" são indistinguíveis daqui — e é
    esse o modo de falha desejado: a resposta não revela dado alheio."""
    _, tenant_b = tenants
    alheia = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO bank_connections (id, tenant_id, pluggy_item_id, status) "
            "VALUES (:id, :t, :i, 'UPDATED')"
        ),
        {"id": str(alheia), "t": str(tenant_b), "i": str(uuid.uuid4())},
    )
    await admin_session.commit()

    async with api() as client:
        listed = await client.get("/connections")
        fetched = await client.get(f"/connections/{alheia}")

    assert listed.json() == []
    assert fetched.status_code == 404
