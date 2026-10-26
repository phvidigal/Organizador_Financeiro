"""`GET /pluggy/diagnostics`: desligado por padrão e, ligado, sem dado pessoal.

Os payloads falsos de `tests/pluggy_fixtures.py` carregam um CPF e um nome em
`/accounts` e em `paymentData` — exatamente o que a resposta crua da Pluggy traz.
Os testes procuram esses valores no corpo devolvido, e não a ausência de uma
chave: o vazamento que importa é o do valor, venha por onde vier.
"""

import json

import httpx
import pytest

from app.api.deps import get_pluggy_client
from app.api.v1 import pluggy_diagnostics
from app.core.config import get_settings
from app.main import create_app
from tests.pluggy_fixtures import (
    ITEM_ID,
    TRANSACTION_WITH_PERSONAL_DATA_JSON,
    default_routes,
    make_client,
)

# Mesmo event loop das fixtures de sessão (ver pyproject.toml).
pytestmark = pytest.mark.asyncio(loop_scope="session")

PERSONAL_VALUES = ("123.456.789-00", "987.654.321-00", "Fulano de Tal", "Ciclano Beltrano")


@pytest.fixture
def api(monkeypatch):
    def build(*, enabled: bool):
        app = create_app()
        settings = get_settings().model_copy(update={"enable_pluggy_diagnostics": enabled})
        app.dependency_overrides[get_settings] = lambda: settings

        routes = default_routes()
        routes[("GET", "/v2/transactions")] = (
            '{"results": [' + TRANSACTION_WITH_PERSONAL_DATA_JSON + '], "page": 1, '
            '"total": 1, "next": null}'
        )
        app.dependency_overrides[get_pluggy_client] = lambda: make_client(routes)

        # O teste de `/auth` usa um `httpx.AsyncClient` próprio, sem transporte
        # injetável — e não é o que está sob teste aqui.
        async def fake_probe(_settings):
            return {"status_code": 200, "campos": ["apiKey"]}

        monkeypatch.setattr(pluggy_diagnostics, "_probe_auth", fake_probe)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        )

    return build


async def test_disabled_by_default_answers_404_before_anything_else(api) -> None:
    """404, e não o 503 de "credenciais da Pluggy faltando": desligado, o endpoint
    não anuncia que existe."""
    async with api(enabled=False) as client:
        response = await client.get("/pluggy/diagnostics", params={"item_id": str(ITEM_ID)})

    assert response.status_code == 404


async def test_enabled_dump_carries_no_personal_data(api) -> None:
    async with api(enabled=True) as client:
        response = await client.get("/pluggy/diagnostics", params={"item_id": str(ITEM_ID)})

    assert response.status_code == 200, response.text
    body = response.text
    for value in PERSONAL_VALUES:
        assert value not in body, value

    # E continua servindo para o que existe: o lado a lado do sinal do cartão.
    contas = json.loads(body)["contas"]
    assert contas and all("transacoes" in conta for conta in contas)
