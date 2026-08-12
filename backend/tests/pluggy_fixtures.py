"""Payloads e transporte falso da Pluggy, compartilhados pelos testes.

Sem o prefixo `test_` de propósito: é módulo de apoio, o pytest não deve coletá-lo.

Os payloads são **strings de JSON cru**, não dicts Python. A diferença importa: o
cliente parseia com `parse_float=Decimal`, e um dict literal já traria `Decimal`
(ou `float`) pronto, escondendo justamente o comportamento que se quer testar.

A heterogeneidade das transações é intencional — uma sem `merchant`, uma sem
`categoryId`, uma `PENDING`. É o formato que quebra `upsert_external_transactions`
se o mapeador omitir chave (ele inspeciona só `rows[0].keys()`).
"""

import uuid
from collections.abc import Callable
from typing import Any

import httpx

ITEM_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
BANK_ACCOUNT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
CREDIT_ACCOUNT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")

API_KEY = "chave-de-teste"
AUTH_RESPONSE = '{"apiKey": "chave-de-teste"}'


ITEM_JSON = """
{
  "id": "11111111-1111-1111-1111-111111111111",
  "connector": {"id": 201, "name": "Banco de Teste", "institutionUrl": "https://exemplo"},
  "status": "UPDATED",
  "executionStatus": "SUCCESS",
  "error": null,
  "products": ["ACCOUNTS", "CREDIT_CARDS", "TRANSACTIONS"],
  "createdAt": "2026-07-01T10:00:00.000Z",
  "updatedAt": "2026-08-10T12:00:00.000Z",
  "lastUpdatedAt": "2026-08-10T12:00:00.000Z",
  "nextAutoSyncAt": "2026-08-11T09:00:00.000Z",
  "consentExpiresAt": "2027-07-01T10:00:00.000Z",
  "clientUserId": null
}
"""

ACCOUNTS_JSON = """
{
  "results": [
    {
      "id": "22222222-2222-2222-2222-222222222222",
      "itemId": "11111111-1111-1111-1111-111111111111",
      "type": "BANK",
      "subtype": "CHECKING_ACCOUNT",
      "number": "1234-5",
      "name": "Conta Corrente",
      "marketingName": "Conta do Dia a Dia",
      "balance": 2500.75,
      "currencyCode": "BRL",
      "taxNumber": "123.456.789-00",
      "owner": "Fulano de Tal",
      "bankData": {"transferNumber": "0001/1234-5", "closingBalance": 2500.75},
      "creditData": null
    },
    {
      "id": "33333333-3333-3333-3333-333333333333",
      "itemId": "11111111-1111-1111-1111-111111111111",
      "type": "CREDIT",
      "subtype": "CREDIT_CARD",
      "number": "**** 4321",
      "name": "Cartao de Credito",
      "marketingName": null,
      "balance": 431.20,
      "currencyCode": "BRL",
      "taxNumber": "123.456.789-00",
      "owner": "Fulano de Tal",
      "bankData": null,
      "creditData": {"level": "GOLD", "brand": "VISA", "creditLimit": 10000.00}
    }
  ]
}
"""

# Página 1 da conta corrente. Traz `next`, então o cliente tem de pedir a página 2.
#
# A primeira transação é a mais completa de propósito: se o mapeador omitisse
# chaves ausentes, o lote passaria porque `rows[0]` tem tudo — e o bug apareceria
# só em produção, com outra ordenação.
TRANSACTIONS_PAGE_1_JSON = """
{
  "results": [
    {
      "id": "aaaaaaa1-0000-0000-0000-000000000001",
      "accountId": "22222222-2222-2222-2222-222222222222",
      "description": "Padaria do Ze",
      "descriptionRaw": "PADARIA DO ZE LTDA  SAO PAULO BR",
      "amount": 12.34,
      "type": "DEBIT",
      "date": "2026-08-01T00:00:00.000Z",
      "status": "POSTED",
      "category": "Groceries",
      "categoryId": "05000000",
      "currencyCode": "BRL",
      "balance": 2513.09,
      "merchant": {"name": "Padaria do Ze", "businessName": "PADARIA DO ZE LTDA"},
      "providerCode": "123",
      "operationType": "PURCHASE",
      "createdAt": "2026-08-01T03:11:00.000Z",
      "updatedAt": "2026-08-01T03:11:00.000Z"
    },
    {
      "id": "aaaaaaa1-0000-0000-0000-000000000002",
      "accountId": "22222222-2222-2222-2222-222222222222",
      "description": "PIX RECEBIDO",
      "descriptionRaw": null,
      "amount": 1500.00,
      "type": "CREDIT",
      "date": "2026-08-02T00:00:00.000Z",
      "status": "POSTED",
      "category": null,
      "categoryId": null,
      "currencyCode": "BRL",
      "createdAt": "2026-08-02T14:00:00.000Z"
    }
  ],
  "page": 1,
  "total": 3,
  "next": "https://api.pluggy.ai/v2/transactions?accountId=22222222-2222-2222-2222-222222222222&after=cursor-pagina-2"
}
"""

# Página 2: sem `next`, então a paginação para. A transação está PENDING, o que
# tem de resultar em `posted_at = NULL`.
TRANSACTIONS_PAGE_2_JSON = """
{
  "results": [
    {
      "id": "aaaaaaa1-0000-0000-0000-000000000003",
      "accountId": "22222222-2222-2222-2222-222222222222",
      "description": "COMPRA PENDENTE",
      "amount": 79.90,
      "type": "DEBIT",
      "date": "2026-08-03T00:00:00.000Z",
      "status": "PENDING",
      "currencyCode": "BRL"
    }
  ],
  "page": 2,
  "total": 3,
  "next": null
}
"""

# Cartão de crédito: é aqui que a suspeita de inversão de sinal se resolve.
TRANSACTIONS_CREDIT_JSON = """
{
  "results": [
    {
      "id": "bbbbbbb1-0000-0000-0000-000000000001",
      "accountId": "33333333-3333-3333-3333-333333333333",
      "description": "RESTAURANTE XYZ",
      "descriptionRaw": "RESTAURANTE XYZ           SAO PAULO",
      "amount": 89.90,
      "type": "DEBIT",
      "date": "2026-08-04T00:00:00.000Z",
      "status": "POSTED",
      "category": "Restaurants",
      "categoryId": "05010000",
      "currencyCode": "BRL",
      "creditCardMetadata": {"billId": "fatura-set", "installmentNumber": null},
      "createdAt": "2026-08-04T20:00:00.000Z"
    }
  ],
  "page": 1,
  "total": 1,
  "next": null
}
"""

CATEGORIES_JSON = """
{
  "results": [
    {"id": "05000000", "description": "Food and drinks",
     "descriptionTranslated": "Alimentacao", "parentId": null, "parentDescription": null},
    {"id": "05010000", "description": "Restaurants",
     "descriptionTranslated": "Restaurantes", "parentId": "05000000",
     "parentDescription": "Food and drinks"},
    {"id": "01000000", "description": "Income",
     "descriptionTranslated": "Receitas", "parentId": null, "parentDescription": null}
  ]
}
"""


Route = str | httpx.Response | Callable[[httpx.Request], "httpx.Response | str"]


def make_pluggy_transport(
    *,
    routes: dict[tuple[str, str], Route],
    auth_response: Route = AUTH_RESPONSE,
    on_request: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """Transporte falso da Pluggy.

    `httpx.MockTransport` já vem no httpx — nenhuma dependência de mock precisa ser
    adicionada ao projeto, e a injeção do `AsyncClient` que ele exige é o mesmo
    mecanismo que o teste ponta a ponta do sync usa.

    `on_request`, quando passado, acumula as requisições: é como se assere "só
    autenticou uma vez" ou "não re-tentou o PATCH".
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if on_request is not None:
            on_request.append(request)

        route: Route | None
        if request.url.path == "/auth":
            route = auth_response
        else:
            route = routes.get((request.method, request.url.path))

        if route is None:
            return httpx.Response(
                404, json={"message": f"sem rota para {request.method} {request.url.path}"}
            )

        if callable(route):
            route = route(request)
        if isinstance(route, httpx.Response):
            return route
        return httpx.Response(
            200, content=route.encode("utf-8"), headers={"content-type": "application/json"}
        )

    return httpx.MockTransport(handler)


def paginated_transactions(request: httpx.Request) -> str:
    """Rota de `/v2/transactions` que respeita `accountId` e o cursor `after`."""
    params = request.url.params
    account_id = params.get("accountId")
    if account_id == str(CREDIT_ACCOUNT_ID):
        return TRANSACTIONS_CREDIT_JSON
    if params.get("after") == "cursor-pagina-2":
        return TRANSACTIONS_PAGE_2_JSON
    return TRANSACTIONS_PAGE_1_JSON


def default_routes() -> dict[tuple[str, str], Route]:
    """Item saudável, duas contas, três transações na conta e uma no cartão."""
    return {
        ("GET", f"/items/{ITEM_ID}"): ITEM_JSON,
        ("PATCH", f"/items/{ITEM_ID}"): ITEM_JSON,
        ("GET", "/accounts"): ACCOUNTS_JSON,
        ("GET", "/v2/transactions"): paginated_transactions,
        ("GET", "/categories"): CATEGORIES_JSON,
    }


def make_client(
    routes: dict[tuple[str, str], Route] | None = None,
    **kwargs: Any,
) -> Any:
    """`PluggyClient` apontado para um transporte falso.

    Import tardio para o módulo continuar utilizável em teste puro (sem app).
    """
    from app.services.pluggy.client import PluggyClient

    transport = make_pluggy_transport(
        routes=routes if routes is not None else default_routes(), **kwargs
    )
    return PluggyClient(
        client_id="id-de-teste",
        client_secret="segredo-de-teste",
        base_url="https://api.pluggy.test",
        http=httpx.AsyncClient(transport=transport),
    )
