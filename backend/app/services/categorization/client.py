"""Cliente HTTP do Ollama.

Camada mais baixa da Fase 3: sabe montar a requisição de chat com saída
estruturada, re-tentar erro transitório e devolver o conteúdo cru. Interpretar o
conteúdo é trabalho de `prompt.parse_response`, e decidir o que fazer com ele é de
`decide.py`.

Estrutura copiada de `app/services/pluggy/client.py` de propósito — `AsyncClient`
injetável, `httpx.Timeout` de módulo, laço de retry explícito. O motivo é o mesmo
lá e aqui: é isso que permite testar com `httpx.MockTransport`, sem rede e sem
dependência de mock nova.
"""

import asyncio
import json
import logging
from decimal import Decimal
from typing import Any

import httpx

from app.services.categorization.errors import (
    OllamaResponseError,
    OllamaUnavailableError,
)

logger = logging.getLogger(__name__)

# Read de 120 s, e não os 5 s de `/health/ollama`: aquele endpoint só lista
# modelos, este espera uma geração. Num 9B com GPU dedicada a resposta sai em
# poucos segundos, mas o Ollama **serializa requisições por padrão** — a segunda
# chamada espera a primeira terminar, e um carregamento de modelo frio soma mais
# um punhado de segundos na primeira de todas.
_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
# Esperas *entre* as tentativas: 3 tentativas gastam no máximo 2 esperas.
_BACKOFF_SECONDS = (0.5, 1.5)

# Trecho do corpo que vai para a mensagem de erro. Curto de propósito: uma geração
# inteira no log não ajuda a depurar e polui a saída do job.
_ERROR_EXCERPT = 300


def _loads(content: bytes | str) -> Any:
    """Parse com `parse_float=Decimal`.

    Mesma razão do cliente da Pluggy: a `confidence` vai para uma coluna
    `NUMERIC(4,3)`, e `Decimal(0.85)` não é 0.85 — o erro entra na primeira
    conversão, não na segunda.
    """
    text = content.decode("utf-8") if isinstance(content, bytes) else content
    return json.loads(text, parse_float=Decimal)


class OllamaClient:
    """Cliente do Ollama do host, usado para categorizar uma transação por chamada.

    Sem autenticação: o Ollama roda em `host.docker.internal:11434`, dentro da
    máquina do usuário, e não tem credencial para gerenciar. É a diferença mais
    visível em relação ao cliente da Pluggy.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._http = http or httpx.AsyncClient(timeout=_TIMEOUT)
        self._owns_http = http is None

    @property
    def model(self) -> str:
        return self._model

    async def aclose(self) -> None:
        """Fecha o pool de conexões, se este cliente for o dono dele."""
        if self._owns_http:
            await self._http.aclose()

    async def chat(self, *, messages: list[dict[str, str]], format_schema: dict[str, Any]) -> str:
        """`POST /api/chat` com saída restrita por JSON Schema.

        Devolve `message.content` cru — uma string que *deveria* ser JSON. Quem a
        interpreta é `prompt.parse_response`, que é puro e testável sem rede.

        O `format` recebe um **JSON Schema completo**, não a string `"json"`: com o
        schema, o Ollama restringe a gramática da geração e a categoria devolvida
        só pode ser uma das que mandamos no `enum`. Sem ele, a resposta seria texto
        a ser adivinhado.
        """
        payload = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            # Extração determinística: é a recomendação da própria documentação de
            # saída estruturada, e é o que faz duas execuções sobre a mesma
            # transação darem o mesmo resultado — sem isso não há como comparar o
            # efeito de uma mudança de prompt.
            "options": {"temperature": 0},
            # O qwen3 é modelo de raciocínio. Pensar antes de responder não melhora
            # a escolha de uma categoria dentro de uma lista fechada e multiplica o
            # tempo de geração por transação. Nem todo modelo honra o pedido, e é
            # por isso que `prompt.parse_response` ainda remove um `<think>` do
            # começo do conteúdo.
            "think": False,
            "format": format_schema,
        }

        response = await self._send(payload)

        if response.status_code >= 400:
            body = self._safe_text(response)
            if response.status_code >= 500:
                raise OllamaUnavailableError(
                    f"Ollama respondeu {response.status_code} em POST /api/chat: {body}"
                )
            raise OllamaResponseError(
                f"Ollama respondeu {response.status_code} em POST /api/chat: {body}"
            )

        try:
            body = _loads(response.content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise OllamaResponseError(
                f"corpo de POST /api/chat não é JSON: {self._safe_text(response)}"
            ) from exc

        content = (body or {}).get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            # Modelo inexistente é o caso comum aqui: o Ollama responde 404 com
            # "model not found", mas uma versão que responda 200 sem conteúdo cairia
            # neste ramo. Diagnóstico é `GET /health/ollama`.
            raise OllamaResponseError(
                f"resposta de POST /api/chat sem message.content utilizável "
                f"(modelo {self._model!r}); recebi {str(body)[:_ERROR_EXCERPT]}"
            )

        return content

    async def _send(self, payload: dict[str, Any]) -> httpx.Response:
        """Envia com re-tentativa em erro transitório.

        Laço explícito, como no cliente da Pluggy: quinze linhas contra uma
        dependência nova, e a política fica legível onde é aplicada.
        """
        url = f"{self._base_url}/api/chat"
        last_exc: Exception | None = None

        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = await self._http.post(url, json=payload, timeout=_TIMEOUT)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt == _MAX_ATTEMPTS - 1:
                    raise OllamaUnavailableError(
                        f"falha de rede em POST {url}: {exc}"
                    ) from exc
                await asyncio.sleep(_BACKOFF_SECONDS[attempt])
                continue

            if response.status_code in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_BACKOFF_SECONDS[attempt])
                continue
            return response

        # Inalcançável: o laço só sai por `return` ou por `raise`.
        raise OllamaUnavailableError(f"tentativas esgotadas em POST {url}: {last_exc}")

    @staticmethod
    def _safe_text(response: httpx.Response) -> str:
        return response.text[:_ERROR_EXCERPT]
