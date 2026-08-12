"""Sync da Pluggy ponta a ponta: resposta HTTP → banco, sem atalho.

Este módulo existe porque os testes de `ingestion.py` exercitam aquele módulo
**diretamente**. Um sync que escrevesse o próprio `INSERT INTO transactions`
passaria verde em todos eles e quebraria em silêncio as três regras que só o
upsert carrega — preservar categoria manual, ressuscitar linha soft-deleted e
derivar `kind`. Só o caminho completo pega isso.

A sessão vem de `app_tenant_session`, que conecta como `app_user`: o sync roda sob
RLS em produção, e testar com a conexão de owner esconderia uma policy faltando ou
um `set_tenant_scope` esquecido na task de background.
"""

import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import CategorizationStatus, ConnectionStatus, TransactionKind
from app.services.pluggy.sync import sync_connection
from tests.conftest import TENANT_A
from tests.pluggy_fixtures import (
    BANK_ACCOUNT_ID,
    CREDIT_ACCOUNT_ID,
    ITEM_ID,
    ITEM_JSON,
    TRANSACTIONS_CREDIT_JSON,
    TRANSACTIONS_PAGE_1_JSON,
    TRANSACTIONS_PAGE_2_JSON,
    default_routes,
    make_client,
)

# Mesmo event loop das fixtures de sessão (ver pyproject.toml).
pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture
async def connection_a(admin_session: AsyncSession, tenants) -> uuid.UUID:
    """Conexão bancária do tenant A apontando para o item das fixtures."""
    tenant_a, _ = tenants
    connection_id = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO bank_connections (id, tenant_id, pluggy_item_id, status) "
            "VALUES (:id, :tenant_id, :item_id, 'UPDATING')"
        ),
        {"id": str(connection_id), "tenant_id": str(tenant_a), "item_id": str(ITEM_ID)},
    )
    await admin_session.commit()
    return connection_id


async def run_sync(connection_id, session_scope, routes=None, **kwargs):
    """Atalho: um sync completo com o transporte falso."""
    client = make_client(routes)
    return await sync_connection(
        tenant_id=TENANT_A,
        connection_id=connection_id,
        client=client,
        session_scope=session_scope,
        **kwargs,
    )


async def fetch_transactions(session_scope) -> list:
    async with session_scope(TENANT_A) as session:
        result = await session.execute(
            text(
                "SELECT external_id, amount, kind, date, posted_at, description_raw, "
                "       pluggy_category_id, pluggy_category_name, categorization_status, "
                "       category_source, currency_code, raw_payload "
                "FROM transactions ORDER BY external_id"
            )
        )
        return list(result)


# --- O caminho feliz ---------------------------------------------------------


async def test_sync_writes_the_pluggy_response_to_the_database(
    connection_a, app_tenant_session
) -> None:
    """Item → contas → transações, exatamente como a Pluggy respondeu."""
    outcome = await run_sync(connection_a, app_tenant_session)

    assert outcome.status == "SUCCESS", outcome.error
    assert outcome.accounts_synced == 2
    assert outcome.transactions_upserted == 4
    # None = nem tentado. 🔬 `PATCH /items` é recusado no tier pessoal, então o
    # padrão é não gastar uma chamada nele — ver REQUEST_REFRESH_BY_DEFAULT.
    assert outcome.item_refresh_supported is None

    async with app_tenant_session(TENANT_A) as session:
        accounts = list(
            await session.execute(
                text("SELECT pluggy_account_id, type, subtype, name, balance FROM accounts")
            )
        )
        connection = (
            await session.execute(
                text(
                    "SELECT status, execution_status, connector_id, connector_name, "
                    "       last_synced_at, last_success_at, consent_expires_at, "
                    "       next_auto_sync_at, error "
                    "FROM bank_connections WHERE id = :id"
                ),
                {"id": str(connection_a)},
            )
        ).one()

    by_id = {row.pluggy_account_id: row for row in accounts}
    assert by_id[BANK_ACCOUNT_ID].type == "BANK"
    assert by_id[BANK_ACCOUNT_ID].subtype == "CHECKING_ACCOUNT"
    assert by_id[BANK_ACCOUNT_ID].balance == Decimal("2500.75")
    assert by_id[CREDIT_ACCOUNT_ID].type == "CREDIT"

    assert connection.status == ConnectionStatus.UPDATED
    assert connection.execution_status == "SUCCESS"
    assert connection.connector_id == 201
    assert connection.connector_name == "Banco de Teste"
    assert connection.last_synced_at is not None
    assert connection.last_success_at is not None
    assert connection.consent_expires_at is not None
    # É o que a interface mostra no lugar de "atualizar agora": o tier pessoal não
    # aceita o PATCH, então quem dita a frequência é a Pluggy.
    assert connection.next_auto_sync_at is not None
    assert connection.error is None


async def test_amounts_keep_the_sign_convention_and_the_exact_scale(
    connection_a, app_tenant_session
) -> None:
    """Negativo = saída, sempre. E `NUMERIC(18,2)` do começo ao fim: se o valor
    tivesse passado por float em qualquer ponto, `-12.34` não voltaria exato."""
    await run_sync(connection_a, app_tenant_session)
    rows = {row.external_id: row for row in await fetch_transactions(app_tenant_session)}

    assert rows["aaaaaaa1-0000-0000-0000-000000000001"].amount == Decimal("-12.34")
    assert rows["aaaaaaa1-0000-0000-0000-000000000002"].amount == Decimal("1500.00")
    # Compra no cartão é saída, com a convenção atual (⚠️ CREDIT_SIGN).
    assert rows["bbbbbbb1-0000-0000-0000-000000000001"].amount == Decimal("-89.90")


async def test_kind_is_derived_by_ingestion_and_transfer_is_never_guessed(
    connection_a, app_tenant_session
) -> None:
    """O sync não informa `kind`; quem deriva do sinal é `ingestion.py`."""
    await run_sync(connection_a, app_tenant_session)
    rows = {row.external_id: row for row in await fetch_transactions(app_tenant_session)}

    assert rows["aaaaaaa1-0000-0000-0000-000000000001"].kind == TransactionKind.EXPENSE
    assert rows["aaaaaaa1-0000-0000-0000-000000000002"].kind == TransactionKind.INCOME
    assert all(row.kind != TransactionKind.TRANSFER for row in rows.values())


async def test_pluggy_category_is_stored_and_the_queue_stays_full(
    connection_a, app_tenant_session
) -> None:
    """A entrega (c) da Fase 2: guardar o palpite da Pluggy sem adotá-lo.

    Adotar aqui esvaziaria o `ix_transactions_pending` e destruiria a régua que a
    Fase 3 usa para medir se a categorização nativa bastaria.
    """
    await run_sync(connection_a, app_tenant_session)
    rows = {row.external_id: row for row in await fetch_transactions(app_tenant_session)}

    padaria = rows["aaaaaaa1-0000-0000-0000-000000000001"]
    assert padaria.pluggy_category_id == "05000000"
    assert padaria.pluggy_category_name == "Groceries"

    assert all(r.categorization_status == CategorizationStatus.PENDING for r in rows.values())
    assert all(r.category_source is None for r in rows.values())


async def test_dates_and_pending_status_survive_the_round_trip(
    connection_a, app_tenant_session
) -> None:
    await run_sync(connection_a, app_tenant_session)
    rows = {row.external_id: row for row in await fetch_transactions(app_tenant_session)}

    # Meia-noite UTC não pode retroceder para 31/07.
    assert rows["aaaaaaa1-0000-0000-0000-000000000001"].date.isoformat() == "2026-08-01"
    assert rows["aaaaaaa1-0000-0000-0000-000000000001"].posted_at is not None
    # A terceira transação está PENDING: ainda não compensou.
    assert rows["aaaaaaa1-0000-0000-0000-000000000003"].posted_at is None


async def test_raw_payload_is_readable_back_with_full_precision(
    connection_a, app_tenant_session
) -> None:
    """`raw_payload` existe para permitir reprocessar. Um `Decimal` cru quebraria a
    gravação do JSONB; um float destruiria a precisão que o parse conquistou."""
    await run_sync(connection_a, app_tenant_session)
    rows = {row.external_id: row for row in await fetch_transactions(app_tenant_session)}

    payload = rows["aaaaaaa1-0000-0000-0000-000000000001"].raw_payload
    assert payload["amount"] == "12.34"
    assert payload["merchant"]["name"] == "Padaria do Ze"


# --- Re-sincronização --------------------------------------------------------


def _routes_with_corrected_transaction() -> dict:
    """A Pluggy corrige a mesma transação: PENDING vira POSTED e o valor muda.

    É o comportamento real que motiva o upsert existir em vez de um insert.
    """
    corrigida = (
        TRANSACTIONS_PAGE_2_JSON.replace('"amount": 79.90', '"amount": 82.50')
        .replace('"status": "PENDING"', '"status": "POSTED"')
    )

    def transactions(request: httpx.Request) -> str:
        params = request.url.params
        if params.get("accountId") == str(CREDIT_ACCOUNT_ID):
            return TRANSACTIONS_CREDIT_JSON
        if params.get("after") == "cursor-pagina-2":
            return corrigida
        return TRANSACTIONS_PAGE_1_JSON

    return {**default_routes(), ("GET", "/v2/transactions"): transactions}


async def test_resync_updates_in_place_without_duplicating(
    connection_a, app_tenant_session
) -> None:
    await run_sync(connection_a, app_tenant_session)
    await run_sync(connection_a, app_tenant_session, _routes_with_corrected_transaction())

    rows = await fetch_transactions(app_tenant_session)
    assert len(rows) == 4  # e não 8

    corrigida = next(r for r in rows if r.external_id.endswith("003"))
    assert corrigida.amount == Decimal("-82.50")
    # Compensou entre um sync e outro.
    assert corrigida.posted_at is not None


async def test_resync_preserves_a_manual_categorization(
    connection_a, admin_session, tenants, app_tenant_session
) -> None:
    """A regressão que um `INSERT` próprio no sync causaria.

    Quando o usuário corrige uma categorização à mão, essa correção é o dado mais
    valioso do sistema — é a base de regras e de treino das Fases 3 e 4. Uma
    re-sincronização sobrescrevendo-a com o palpite da Pluggy apagaria esse
    trabalho sem erro, sem log, e só perceptível ao reabrir a tela semanas depois.
    """
    tenant_a, _ = tenants
    category_id = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO categories (id, tenant_id, name, kind) "
            "VALUES (:id, :tenant_id, 'Transferência de teste', 'TRANSFER')"
        ),
        {"id": str(category_id), "tenant_id": str(tenant_a)},
    )
    await admin_session.commit()

    await run_sync(connection_a, app_tenant_session)

    # O usuário corrige na tela de revisão: categoria, origem e natureza.
    async with app_tenant_session(TENANT_A) as session:
        await session.execute(
            text(
                "UPDATE transactions SET category_id = :c, category_source = 'MANUAL', "
                "categorization_status = 'CATEGORIZED', kind = 'TRANSFER' "
                "WHERE external_id = :e"
            ),
            {"c": str(category_id), "e": "aaaaaaa1-0000-0000-0000-000000000002"},
        )

    # A Pluggy re-sincroniza insistindo no que ela acha.
    await run_sync(connection_a, app_tenant_session, _routes_with_corrected_transaction())

    async with app_tenant_session(TENANT_A) as session:
        row = (
            await session.execute(
                text(
                    "SELECT category_id, category_source, categorization_status, kind, amount "
                    "FROM transactions WHERE external_id = :e"
                ),
                {"e": "aaaaaaa1-0000-0000-0000-000000000002"},
            )
        ).one()

    assert row.category_id == category_id
    assert row.category_source == "MANUAL"
    assert row.categorization_status == CategorizationStatus.CATEGORIZED
    # `kind` anda junto: revertê-lo para INCOME traria o valor de volta ao total
    # sem nenhum sinal de que algo mudou.
    assert row.kind == TransactionKind.TRANSFER
    # E o que é fato reportado pelo banco continua sendo atualizado.
    assert row.amount == Decimal("1500.00")


async def test_second_sync_asks_only_for_what_was_created_since(
    connection_a, app_tenant_session
) -> None:
    """Watermark incremental: `createdAtFrom`, com folga de um dia."""
    await run_sync(connection_a, app_tenant_session)

    seen: list[httpx.Request] = []
    client = make_client(on_request=seen)
    await sync_connection(
        tenant_id=TENANT_A,
        connection_id=connection_a,
        client=client,
        session_scope=app_tenant_session,
    )

    first_page = [r for r in seen if r.url.path == "/v2/transactions"][0]
    assert "createdAtFrom" in first_page.url.params
    assert "dateFrom" not in first_page.url.params


async def test_full_sync_ignores_the_watermark(connection_a, app_tenant_session) -> None:
    """O botão que reescreve o histórico depois de corrigir a convenção de sinal do
    cartão — sem migração de dados, porque `amount` é coluna sincronizável."""
    await run_sync(connection_a, app_tenant_session)

    seen: list[httpx.Request] = []
    client = make_client(on_request=seen)
    await sync_connection(
        tenant_id=TENANT_A,
        connection_id=connection_a,
        client=client,
        session_scope=app_tenant_session,
        full=True,
    )

    first_page = [r for r in seen if r.url.path == "/v2/transactions"][0]
    assert "createdAtFrom" not in first_page.url.params


# --- Falha ------------------------------------------------------------------


async def test_pluggy_failure_is_recorded_on_the_connection(
    connection_a, app_tenant_session
) -> None:
    """Falha nossa (rede, 5xx) vira `status = ERROR` com o motivo em `error`, e
    nenhuma transação órfã fica para trás."""
    routes = {
        **default_routes(),
        ("GET", f"/items/{ITEM_ID}"): httpx.Response(500, text="boom"),
        ("PATCH", f"/items/{ITEM_ID}"): httpx.Response(500, text="boom"),
    }
    outcome = await run_sync(connection_a, app_tenant_session, routes)

    assert outcome.status == "FAILED"
    assert outcome.error is not None

    async with app_tenant_session(TENANT_A) as session:
        row = (
            await session.execute(
                text("SELECT status, error, last_success_at FROM bank_connections WHERE id = :id"),
                {"id": str(connection_a)},
            )
        ).one()
        count = await session.scalar(text("SELECT count(*) FROM transactions"))

    assert row.status == ConnectionStatus.ERROR
    assert row.error["type"] == "PluggyUnavailableError"
    assert "GET /items" in row.error["phase"]
    # O marco não avançou: o próximo disparo relê a mesma janela.
    assert row.last_success_at is None
    assert count == 0


async def test_login_error_stops_before_reading_stale_accounts(
    connection_a, app_tenant_session
) -> None:
    """Seguir lendo devolveria o dado velho que já está no banco e mascararia a
    única coisa que importa: o usuário precisa reconectar."""
    item = ITEM_JSON.replace('"status": "UPDATED"', '"status": "LOGIN_ERROR"').replace(
        '"error": null', '"error": {"code": "INVALID_CREDENTIALS", "message": "senha"}'
    )
    routes = {**default_routes(), ("GET", f"/items/{ITEM_ID}"): item}

    outcome = await run_sync(connection_a, app_tenant_session, routes)

    assert outcome.status == "PARTIAL"
    assert outcome.accounts_synced == 0

    async with app_tenant_session(TENANT_A) as session:
        row = (
            await session.execute(
                text("SELECT status, error FROM bank_connections WHERE id = :id"),
                {"id": str(connection_a)},
            )
        ).one()

    # O status é o que a Pluggy reportou, não um ERROR genérico: é o que permite à
    # interface dizer "reconecte sua conta" em vez de "algo deu errado".
    assert row.status == ConnectionStatus.LOGIN_ERROR
    assert row.error["code"] == "INVALID_CREDENTIALS"


async def test_no_refresh_is_requested_by_default(connection_a, app_tenant_session) -> None:
    """🔬 `PATCH /items` responde `400 "MeuPluggy item cant be updated"` no tier
    pessoal. Tentar a cada sync seria uma chamada garantidamente inútil."""
    seen: list[httpx.Request] = []
    client = make_client(on_request=seen)
    await sync_connection(
        tenant_id=TENANT_A,
        connection_id=connection_a,
        client=client,
        session_scope=app_tenant_session,
    )

    assert not [r for r in seen if r.method == "PATCH"]


async def test_refused_item_refresh_does_not_fail_the_sync(
    connection_a, app_tenant_session
) -> None:
    """Quando o refresh É pedido e a Pluggy recusa, não é sync que falhou: é sync
    que segue lendo o que ela já sincronizou por conta própria.

    Vale para o 400 do MeuPluggy e para o 403 de um plano sem permissão.
    """
    routes = {
        **default_routes(),
        ("PATCH", f"/items/{ITEM_ID}"): httpx.Response(
            400, json={"message": "MeuPluggy item cant be updated"}
        ),
    }
    outcome = await run_sync(connection_a, app_tenant_session, routes, request_refresh=True)

    assert outcome.status == "SUCCESS"
    assert outcome.item_refresh_supported is False
    assert outcome.transactions_upserted == 4
    assert any("atualização" in w for w in outcome.warnings)


async def test_unmodelled_account_type_is_skipped_not_fatal(
    connection_a, app_tenant_session
) -> None:
    """Conta de investimento não tem lugar no schema do MVP. Pular uma é melhor que
    perder o sync das outras."""
    from tests.pluggy_fixtures import ACCOUNTS_JSON

    routes = {
        **default_routes(),
        ("GET", "/accounts"): ACCOUNTS_JSON.replace('"type": "CREDIT"', '"type": "INVESTMENT"'),
    }
    outcome = await run_sync(connection_a, app_tenant_session, routes)

    assert outcome.status == "SUCCESS"
    assert outcome.accounts_synced == 1
    assert any("INVESTMENT" in w for w in outcome.warnings)
