"""Cliente HTTP do Ollama — corpo da requisição, retry e erros, sem rede.

`httpx.MockTransport`, como no cliente da Pluggy: nenhuma dependência de mock foi
adicionada ao projeto, e o `AsyncClient` injetável que ele exige é o mesmo
mecanismo que o resto da suíte usa.
"""

import json

import httpx
import pytest

from app.services.categorization import client as client_module
from app.services.categorization.client import OllamaClient
from app.services.categorization.errors import (
    OllamaResponseError,
    OllamaUnavailableError,
)

# Mesmo event loop das fixtures de sessão (ver pyproject.toml).
pytestmark = pytest.mark.asyncio(loop_scope="session")

SCHEMA = {
    "type": "object",
    "properties": {"category": {"type": "string", "enum": ["Alimentação"]}},
}
MESSAGES = [{"role": "user", "content": "categorize"}]


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zera as esperas do retry. Testar a política não exige esperar por ela."""
    monkeypatch.setattr(client_module, "_BACKOFF_SECONDS", (0.0, 0.0))


def make_client(handler) -> tuple[OllamaClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def transport_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    return (
        OllamaClient(
            base_url="http://ollama.test",
            model="qwen3.5:9b",
            http=httpx.AsyncClient(transport=httpx.MockTransport(transport_handler)),
        ),
        requests,
    )


def ok(content: str = '{"category": "Alimentação", "confidence": 0.9}') -> httpx.Response:
    return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})


async def test_request_carries_the_schema_and_zero_temperature() -> None:
    """O `format` com JSON Schema é o que restringe a gramática da geração.

    Sem ele — ou com a string `"json"` no lugar do schema — a resposta volta a ser
    texto a ser adivinhado, e o `enum` de categorias deixa de valer.
    """
    client, requests = make_client(lambda _: ok())

    await client.chat(messages=MESSAGES, format_schema=SCHEMA)

    body = json.loads(requests[0].content)
    assert requests[0].url.path == "/api/chat"
    assert body["format"] == SCHEMA
    assert body["model"] == "qwen3.5:9b"
    assert body["stream"] is False
    # Extração determinística: duas execuções sobre a mesma transação precisam dar
    # o mesmo resultado, senão não há como medir o efeito de mudar o prompt.
    assert body["options"]["temperature"] == 0
    # O qwen3 é modelo de raciocínio; pensar não melhora escolher numa lista fechada.
    assert body["think"] is False


async def test_content_is_returned_raw() -> None:
    """Interpretar o conteúdo é de `prompt.parse_response`, que é puro."""
    client, _ = make_client(lambda _: ok('{"category": "Alimentação", "confidence": 1}'))

    content = await client.chat(messages=MESSAGES, format_schema=SCHEMA)

    assert content == '{"category": "Alimentação", "confidence": 1}'


async def test_transient_error_is_retried() -> None:
    responses = [httpx.Response(503, text="loading model"), ok()]
    client, requests = make_client(lambda _: responses.pop(0))

    await client.chat(messages=MESSAGES, format_schema=SCHEMA)

    assert len(requests) == 2


async def test_persistent_5xx_becomes_unavailable() -> None:
    """Infraestrutura: o job precisa distinguir isto para deixar a linha PENDING."""
    client, requests = make_client(lambda _: httpx.Response(500, text="boom"))

    with pytest.raises(OllamaUnavailableError):
        await client.chat(messages=MESSAGES, format_schema=SCHEMA)

    assert len(requests) == 3


async def test_network_failure_becomes_unavailable() -> None:
    def explode(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client, _ = make_client(explode)

    with pytest.raises(OllamaUnavailableError):
        await client.chat(messages=MESSAGES, format_schema=SCHEMA)


async def test_client_error_is_not_retried() -> None:
    """404 é "modelo não existe": re-tentar não faz o modelo aparecer."""
    client, requests = make_client(lambda _: httpx.Response(404, text="model not found"))

    with pytest.raises(OllamaResponseError):
        await client.chat(messages=MESSAGES, format_schema=SCHEMA)

    assert len(requests) == 1


async def test_response_without_content_is_rejected() -> None:
    client, _ = make_client(lambda _: httpx.Response(200, json={"done": True}))

    with pytest.raises(OllamaResponseError):
        await client.chat(messages=MESSAGES, format_schema=SCHEMA)
