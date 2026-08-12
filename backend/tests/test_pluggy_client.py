"""Cliente HTTP da Pluggy — autenticação, retry e paginação, sem rede.

O transporte é `httpx.MockTransport`, que já vem no httpx: nenhuma dependência de
mock foi adicionada ao projeto. O `AsyncClient` injetável que ele exige é o mesmo
mecanismo que o teste ponta a ponta do sync usa.
"""

from datetime import UTC, datetime

import httpx
import pytest

from app.services.pluggy import client as client_module
from app.services.pluggy.client import PluggyClient
from app.services.pluggy.errors import (
    PluggyAuthError,
    PluggyForbiddenError,
    PluggyNotFoundError,
    PluggyUnavailableError,
)
from tests.pluggy_fixtures import (
    BANK_ACCOUNT_ID,
    ITEM_ID,
    TRANSACTIONS_PAGE_1_JSON,
    default_routes,
    make_client,
)

# Mesmo event loop das fixtures de sessão (ver pyproject.toml).
pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zera as esperas do retry. Testar a política não exige esperar por ela."""
    monkeypatch.setattr(client_module, "_BACKOFF_SECONDS", (0.0, 0.0))


def _auths(requests: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in requests if r.url.path == "/auth"]


# --- Autenticação ------------------------------------------------------------


async def test_authenticates_once_for_many_calls() -> None:
    """A key dura ~2 horas. Pedir uma nova a cada chamada dobraria a latência do
    sync inteiro, que faz dezenas de requisições."""
    seen: list[httpx.Request] = []
    client = make_client(on_request=seen)

    await client.get_item(ITEM_ID)
    await client.list_accounts(ITEM_ID)
    await client.list_categories()

    assert len(_auths(seen)) == 1
    # E a key foi mesmo usada nas chamadas seguintes.
    assert seen[-1].headers["X-API-KEY"] == "chave-de-teste"


async def test_expired_key_is_renewed_once_and_the_call_succeeds() -> None:
    """Key que vence antes do TTL não pode derrubar um sync em andamento."""
    seen: list[httpx.Request] = []
    calls = {"n": 0}

    def item_route(request: httpx.Request) -> httpx.Response | str:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(401, json={"message": "expired api key"})
        return default_routes()[("GET", f"/items/{ITEM_ID}")]

    client = make_client(
        {**default_routes(), ("GET", f"/items/{ITEM_ID}"): item_route}, on_request=seen
    )

    item = await client.get_item(ITEM_ID)

    assert item["status"] == "UPDATED"
    assert len(_auths(seen)) == 2  # a inicial e a renovação


async def test_two_consecutive_401_is_an_auth_error_not_a_loop() -> None:
    client = make_client(
        {
            **default_routes(),
            ("GET", f"/items/{ITEM_ID}"): httpx.Response(401, json={"message": "nope"}),
        }
    )

    with pytest.raises(PluggyAuthError):
        await client.get_item(ITEM_ID)


async def test_auth_response_without_a_known_key_field_lists_only_the_keys() -> None:
    """A mensagem vai para o log, e um dos valores da resposta É a chave."""
    client = make_client(auth_response='{"token": "super-secreto", "expiresIn": 7200}')

    with pytest.raises(PluggyAuthError) as excinfo:
        await client.get_item(ITEM_ID)

    message = str(excinfo.value)
    assert "token" in message
    assert "super-secreto" not in message


async def test_rejected_credentials_do_not_leak_the_body() -> None:
    """Lista de permissão, não de bloqueio: o corpo de um erro de auth pode ecoar o
    que foi enviado."""
    client = make_client(auth_response=httpx.Response(403, json={"clientSecret": "eco-do-envio"}))

    with pytest.raises(PluggyAuthError) as excinfo:
        await client.get_item(ITEM_ID)

    assert "eco-do-envio" not in str(excinfo.value)


async def test_auth_error_keeps_the_reason_for_the_refusal() -> None:
    """"Credenciais recusadas" e "Client is disabled" mandam procurar em lugares
    diferentes: o segundo aponta para o estado da Application no Dashboard, não
    para o `.env`. Perder a distinção custa uma hora de depuração no lugar errado."""
    client = make_client(
        auth_response=httpx.Response(
            401,
            json={
                "message": "Client is disabled",
                "code": 401,
                "codeDescription": "CLIENT_DISABLED",
                "errorId": "7cabfc81",
                "clientSecret": "eco-do-envio",
            },
        )
    )

    with pytest.raises(PluggyAuthError) as excinfo:
        await client.get_item(ITEM_ID)

    message = str(excinfo.value)
    assert "CLIENT_DISABLED" in message
    assert "Client is disabled" in message
    assert "eco-do-envio" not in message


# --- Retry -------------------------------------------------------------------


async def test_transient_5xx_is_retried_and_then_succeeds() -> None:
    seen: list[httpx.Request] = []
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response | str:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"message": "indisponivel"})
        return default_routes()[("GET", f"/items/{ITEM_ID}")]

    client = make_client({**default_routes(), ("GET", f"/items/{ITEM_ID}"): flaky}, on_request=seen)

    item = await client.get_item(ITEM_ID)

    assert item["status"] == "UPDATED"
    assert calls["n"] == 3


async def test_persistent_5xx_becomes_unavailable() -> None:
    client = make_client(
        {**default_routes(), ("GET", f"/items/{ITEM_ID}"): httpx.Response(500, text="boom")}
    )

    with pytest.raises(PluggyUnavailableError):
        await client.get_item(ITEM_ID)


async def test_patch_item_is_never_retried() -> None:
    """`PATCH /items` dispara uma atualização real na instituição e consome cota.
    Não é idempotente: um 504 pode significar "aceito, só demorou", e re-tentar
    seria o caminho para duas atualizações."""
    seen: list[httpx.Request] = []
    client = make_client(
        {**default_routes(), ("PATCH", f"/items/{ITEM_ID}"): httpx.Response(504, text="timeout")},
        on_request=seen,
    )

    with pytest.raises(PluggyUnavailableError):
        await client.refresh_item(ITEM_ID)

    patches = [r for r in seen if r.method == "PATCH"]
    assert len(patches) == 1


async def test_forbidden_patch_is_a_distinct_error() -> None:
    """É a resposta esperada no tier pessoal. O sync trata como "atualização não
    suportada, siga lendo" — não como falha."""
    seen: list[httpx.Request] = []
    client = make_client(
        {**default_routes(), ("PATCH", f"/items/{ITEM_ID}"): httpx.Response(403, json={"c": 1})},
        on_request=seen,
    )

    with pytest.raises(PluggyForbiddenError):
        await client.refresh_item(ITEM_ID)

    # 403 não dispara renovação de key: seria mandar o PATCH de novo à toa.
    assert len([r for r in seen if r.method == "PATCH"]) == 1


async def test_missing_item_is_a_not_found_error() -> None:
    client = make_client({})

    with pytest.raises(PluggyNotFoundError):
        await client.get_item(ITEM_ID)


async def test_network_failure_becomes_unavailable() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sem rota para o host")

    transport = httpx.MockTransport(boom)
    client = PluggyClient(
        client_id="x",
        client_secret="y",
        base_url="https://api.pluggy.test",
        http=httpx.AsyncClient(transport=transport),
    )

    with pytest.raises(PluggyUnavailableError):
        await client.get_item(ITEM_ID)


# --- Paginação ---------------------------------------------------------------


async def test_pagination_follows_next_and_stops() -> None:
    client = make_client()

    pages = [page async for page in client.iter_transactions(BANK_ACCOUNT_ID)]

    assert [len(p) for p in pages] == [2, 1]
    assert pages[1][0]["status"] == "PENDING"


async def test_cursor_is_extracted_not_followed_as_a_url() -> None:
    """Seguir a URL crua deixaria a resposta redirecionar as próximas chamadas para
    um host arbitrário."""
    seen: list[httpx.Request] = []
    client = make_client(on_request=seen)

    _ = [page async for page in client.iter_transactions(BANK_ACCOUNT_ID)]

    second = [r for r in seen if r.url.path == "/v2/transactions"][1]
    assert second.url.host == "api.pluggy.test"
    assert second.url.params["after"] == "cursor-pagina-2"


async def test_repeated_cursor_does_not_loop_forever() -> None:
    """Um cursor que não avança giraria consumindo cota da Pluggy até o processo
    morrer. Parar é o comportamento certo."""
    seen: list[httpx.Request] = []
    client = make_client(
        {**default_routes(), ("GET", "/v2/transactions"): TRANSACTIONS_PAGE_1_JSON},
        on_request=seen,
    )

    pages = [page async for page in client.iter_transactions(BANK_ACCOUNT_ID)]

    # Primeira página, depois o mesmo cursor de volta: para na segunda.
    assert len(pages) == 2
    assert len([r for r in seen if r.url.path == "/v2/transactions"]) == 2


async def test_incremental_filter_uses_created_at_from() -> None:
    """`createdAtFrom` e não `dateFrom`: transação antiga pode ser criada hoje, e
    filtrar pela data do lançamento a perderia."""
    seen: list[httpx.Request] = []
    client = make_client(on_request=seen)

    _ = [
        page
        async for page in client.iter_transactions(
            BANK_ACCOUNT_ID, created_at_from=datetime(2026, 8, 1, 3, 30, 0, tzinfo=UTC)
        )
    ]

    first = [r for r in seen if r.url.path == "/v2/transactions"][0]
    assert first.url.params["createdAtFrom"] == "2026-08-01T03:30:00.000Z"
    assert "dateFrom" not in first.url.params


async def test_only_documented_parameters_are_sent() -> None:
    """🔬 A API valida os parâmetros de forma estrita: qualquer chave fora da lista
    documentada derruba a chamada com `400 "property X should not exist"`.

    Foi assim que um `pageSize` mandado "por via das dúvidas" quebrou o primeiro
    sync real. Nada entra aqui sem estar na doc.
    """
    seen: list[httpx.Request] = []
    client = make_client(on_request=seen)

    _ = [page async for page in client.iter_transactions(BANK_ACCOUNT_ID)]

    first = [r for r in seen if r.url.path == "/v2/transactions"][0]
    assert set(first.url.params) == {"accountId"}
