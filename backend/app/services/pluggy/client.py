"""Cliente HTTP da API da Pluggy.

Camada mais baixa da integração: sabe autenticar, re-tentar, paginar e nada mais.
Devolve o JSON cru — traduzir para colunas é trabalho de `mappers.py`, e gravar é
de `sync.py`. A referência de endpoints está em `docs/pluggy-api.md`.

O `AsyncClient` do httpx é injetável de propósito: é isso que permite testar a
autenticação, o retry e a paginação com `httpx.MockTransport`, sem rede e sem
dependência nova de mock.
"""

import asyncio
import base64
import binascii
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from app.services.pluggy.errors import (
    PluggyAPIError,
    PluggyAuthError,
    PluggyForbiddenError,
    PluggyNotFoundError,
    PluggyUnavailableError,
)

logger = logging.getLogger(__name__)

# Validade presumida, usada só quando não dá para ler a real. A doc fala em ~2
# horas; a margem de 20 minutos evita a corrida em que a key expira no meio de um
# sync longo. O caminho de re-auth em 401 continua existindo como rede de
# segurança, mas não deveria ser o caso comum.
_API_KEY_TTL = timedelta(minutes=100)

# Margem descontada da expiração real declarada no token.
_API_KEY_SKEW = timedelta(minutes=5)

# ✅ Verificado contra a API real: `POST /auth` responde 200 com um único campo,
# `apiKey`, contendo um JWT. Os outros dois nomes ficam como tolerância barata —
# custam uma comparação e evitam quebrar se a Pluggy renomear o campo.
_API_KEY_FIELDS = ("apiKey", "api_key", "accessToken")

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
# Esperas *entre* as tentativas: 3 tentativas gastam no máximo 2 esperas.
_BACKOFF_SECONDS = (0.5, 1.5)

# Teto de páginas de transações. 200 páginas × 500 = 100 mil transações, muito além
# de qualquer histórico pessoal. Existe como fusível: um cursor que não avança
# giraria para sempre consumindo cota da Pluggy.
_MAX_PAGES = 200

# Leitura em 30 s e não nos 5 s de `/health/ollama`: `GET /items` de um item em
# UPDATING e a primeira página de 500 transações são visivelmente lentos.
_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


def _loads(content: bytes) -> Any:
    """Parse do JSON com `parse_float=Decimal`.

    `response.json()` do httpx usa o `json.loads` padrão, que devolve `float` — e
    aí a invariante "dinheiro é NUMERIC(18,2), nunca float" já foi violada antes de
    o valor chegar ao mapeador. Converter depois é tarde: `Decimal(0.1)` é
    0.1000000000000000055511151231257827, enquanto `Decimal("0.1")` é 0.1. O erro
    entra na primeira conversão, não na segunda.

    O efeito colateral é que todo número fracionário do payload vira `Decimal`,
    inclusive dentro de `merchant` e do que vai para `raw_payload` — e `json.dumps`
    não serializa `Decimal`. Quem fecha esse ciclo é `mappers.jsonable`.
    """
    return json.loads(content.decode("utf-8"), parse_float=Decimal)


def _format_created_at_from(value: datetime) -> str:
    """Formato que a Pluggy espera em `createdAtFrom`: `yyyy-mm-ddThh:mm:ss.000Z`."""
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _jwt_expiry(token: str) -> datetime | None:
    """Expiração declarada no próprio token, quando ele for um JWT.

    A resposta de `POST /auth` traz só `apiKey` — nenhum campo de validade. Mas a
    chave é um JWT, e o `exp` dele é a verdade sobre quando vai parar de funcionar.
    Ler daí é melhor que confiar no "~2 horas" da documentação: se a Pluggy encurtar
    a validade, o cliente acompanha sozinho em vez de passar a tomar 401 no meio dos
    syncs.

    Best-effort de propósito — sem verificar assinatura, que é trabalho de quem
    emitiu, e voltando `None` a qualquer sinal de que o formato não é o esperado.
    Nesse caso vale o `_API_KEY_TTL`.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload_segment = parts[1]
        # JWT usa base64url sem padding; o decoder exige múltiplo de 4.
        padding = "=" * (-len(payload_segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_segment + padding))
        exp = payload.get("exp")
    except (ValueError, TypeError, binascii.Error):
        return None

    if not isinstance(exp, int | float):
        return None
    try:
        return datetime.fromtimestamp(exp, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _extract_after(next_link: str) -> str | None:
    """Tira o cursor `after` do campo `next` da resposta.

    A Pluggy pode devolver `next` como URL absoluta ou como query string solta.
    Extrair só o parâmetro, em vez de seguir a URL recebida, mantém o cliente preso
    ao host configurado — uma resposta não deveria conseguir redirecionar as
    próximas chamadas para outro lugar.
    """
    query = urlparse(next_link).query or next_link.lstrip("?")
    values = parse_qs(query).get("after")
    return values[0] if values else None


class PluggyClient:
    """Cliente autenticado da API da Pluggy.

    A instância guarda a API key em memória (nunca em banco: a key vive ~2 horas e
    persistir segredo criaria um objeto a mais para a criptografia da Fase 5).
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        base_url: str = "https://api.pluggy.ai",
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._base_url = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=_TIMEOUT)
        self._owns_http = http is None

        self._key: str | None = None
        self._key_expires_at: datetime | None = None
        self._auth_lock = asyncio.Lock()

    async def aclose(self) -> None:
        """Fecha o pool de conexões, se este cliente for o dono dele."""
        if self._owns_http:
            await self._http.aclose()

    # --- autenticação --------------------------------------------------------

    def _cached_key(self) -> str | None:
        """A key em memória, se ainda estiver dentro da validade."""
        if self._key is None or self._key_expires_at is None:
            return None
        return self._key if datetime.now(UTC) < self._key_expires_at else None

    async def _api_key(self, *, force: bool = False) -> str:
        """Devolve uma API key válida, autenticando só quando necessário.

        O lock com re-checagem interna (double-checked locking) impede que N
        chamadas concorrentes disparem N `POST /auth`: a primeira autentica, as
        demais acordam e encontram a key já pronta.
        """
        if not force and (key := self._cached_key()) is not None:
            return key

        async with self._auth_lock:
            # Outra corrotina pode ter autenticado enquanto esperávamos o lock.
            if not force and (key := self._cached_key()) is not None:
                return key
            key = await self._authenticate()
            self._key = key
            declared = _jwt_expiry(key)
            self._key_expires_at = (
                declared - _API_KEY_SKEW
                if declared is not None
                else datetime.now(UTC) + _API_KEY_TTL
            )
            return key

    async def _authenticate(self) -> str:
        """Troca `clientId`/`clientSecret` pela API key de vida curta.

        Único ponto do projeto que conhece o formato da resposta de `POST /auth` —
        que é justamente a parte de `docs/pluggy-api.md` ainda não confirmada na
        fonte oficial. Se o campo mudar de nome, muda aqui e em lugar nenhum mais.
        """
        url = f"{self._base_url}/auth"
        payload = {"clientId": self._client_id, "clientSecret": self._client_secret}
        try:
            response = await self._http.post(url, json=payload, timeout=_TIMEOUT)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise PluggyUnavailableError(f"falha de rede ao autenticar na Pluggy: {exc}") from exc

        if response.status_code >= 400:
            raise PluggyAuthError(
                f"Pluggy recusou as credenciais em POST /auth "
                f"(HTTP {response.status_code}){_auth_error_hint(response)}"
            )

        body = _loads(response.content)
        if isinstance(body, dict):
            for field in _API_KEY_FIELDS:
                value = body.get(field)
                if isinstance(value, str) and value:
                    return value

        # Lista as CHAVES recebidas, nunca os valores: a mensagem vai para o log e
        # uma delas é a própria API key.
        received = sorted(body) if isinstance(body, dict) else type(body).__name__
        raise PluggyAuthError(
            f"resposta de POST /auth sem campo de chave reconhecível; "
            f"esperava um de {_API_KEY_FIELDS}, recebi {received}"
        )

    # --- transporte ----------------------------------------------------------

    async def _send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None,
        json_body: Any,
        retry: bool,
    ) -> httpx.Response:
        """Envia com re-tentativa em erro transitório.

        Laço explícito em vez de `tenacity`: são quinze linhas, e uma dependência a
        menos vale mais que a ergonomia do decorator — que ainda por cima esconderia
        a política de retry numa configuração que ninguém relê.
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = await self._http.request(
                    method, url, headers=headers, params=params, json=json_body, timeout=_TIMEOUT
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if not retry or attempt == _MAX_ATTEMPTS - 1:
                    raise PluggyUnavailableError(
                        f"falha de rede em {method} {url}: {exc}"
                    ) from exc
                await asyncio.sleep(_BACKOFF_SECONDS[attempt])
                continue

            if retry and response.status_code in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_BACKOFF_SECONDS[attempt])
                continue
            return response

        # Inalcançável: o laço só sai por `return` ou por `raise`.
        raise PluggyUnavailableError(f"tentativas esgotadas em {method} {url}: {last_exc}")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        retry: bool = True,
        _auth_retried: bool = False,
    ) -> Any:
        """Chamada autenticada. Devolve o JSON já parseado."""
        api_key = await self._api_key()
        url = f"{self._base_url}{path}"
        clean_params = (
            {k: v for k, v in params.items() if v is not None} if params is not None else None
        )

        response = await self._send(
            method,
            url,
            headers={"X-API-KEY": api_key},
            params=clean_params,
            json_body=json_body,
            retry=retry,
        )

        if response.status_code == 401 and not _auth_retried:
            # A key pode ter expirado antes do TTL, ou sido revogada. Uma segunda
            # tentativa com key nova desempata — sem ela, um sync de dez minutos
            # morreria inteiro na virada das duas horas. Exatamente uma
            # re-tentativa: 401 duas vezes é credencial errada, não key velha.
            #
            # 403 fica de fora de propósito. Na Pluggy ele significa "seu plano não
            # autoriza", não "sua chave venceu", e re-tentar mandaria um `PATCH
            # /items` — que não é idempotente — pela segunda vez sem necessidade.
            logger.info("Pluggy devolveu 401; renovando a API key")
            await self._api_key(force=True)
            return await self._request(
                method, path, params=params, json_body=json_body, retry=retry, _auth_retried=True
            )

        if response.status_code >= 400:
            body = _safe_body(response)
            if response.status_code == 404:
                raise PluggyNotFoundError(404, path, body)
            if response.status_code == 403:
                raise PluggyForbiddenError(403, path, body)
            if response.status_code in (401,):
                raise PluggyAuthError(f"Pluggy recusou a API key em {method} {path}")
            if response.status_code >= 500:
                raise PluggyUnavailableError(
                    f"Pluggy respondeu {response.status_code} em {method} {path}"
                )
            raise PluggyAPIError(response.status_code, path, body)

        if not response.content:
            return None
        return _loads(response.content)

    # --- endpoints -----------------------------------------------------------

    async def get_item(self, item_id: uuid.UUID) -> dict[str, Any]:
        """`GET /items/{id}` — estado da conexão bancária."""
        return await self._request("GET", f"/items/{item_id}")

    async def refresh_item(self, item_id: uuid.UUID) -> dict[str, Any]:
        """`PATCH /items/{id}` — pede à Pluggy que atualize o item na instituição.

        Sem re-tentativa, ao contrário de todo o resto: isto não é idempotente do
        lado da Pluggy — dispara uma atualização real e consome cota. Um 504 pode
        significar "aceito, só demorou", e re-tentar seria o caminho para duas
        atualizações.

        ⚠️ Pode responder 403 no tier pessoal. Quem chama trata `PluggyForbiddenError`
        como "atualização não suportada", não como falha de sync.
        """
        return await self._request("PATCH", f"/items/{item_id}", retry=False)

    async def list_accounts(self, item_id: uuid.UUID) -> list[dict[str, Any]]:
        """`GET /accounts?itemId=` — contas e cartões do item."""
        payload = await self._request("GET", "/accounts", params={"itemId": str(item_id)})
        return list(payload.get("results") or []) if isinstance(payload, dict) else []

    async def list_categories(self) -> list[dict[str, Any]]:
        """`GET /categories` — taxonomia da Pluggy, insumo do de/para."""
        payload = await self._request("GET", "/categories")
        return list(payload.get("results") or []) if isinstance(payload, dict) else []

    async def iter_transactions(
        self,
        account_id: uuid.UUID,
        *,
        created_at_from: datetime | None = None,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """`GET /v2/transactions` — rende uma lista por página, não transação a transação.

        Note o `/v2`: os outros endpoints não têm prefixo de versão.

        Render páginas e não itens soltos é deliberado: quem consome grava um lote
        por página, numa transação de banco por página. Um iterador de itens
        obrigaria o chamador a reagrupar para conseguir o mesmo efeito.

        O filtro é `createdAtFrom` e não `dateFrom` porque uma transação antiga pode
        ser criada hoje na Pluggy — filtrar pela data do lançamento a perderia.

        🔬 **Não existe controle de tamanho de página.** A API valida os parâmetros
        de forma estrita e responde `400 "property pageSize should not exist"`. Vale
        para qualquer parâmetro fora da lista documentada: aqui não se manda nada
        "por via das dúvidas".
        """
        params: dict[str, Any] = {"accountId": str(account_id)}
        if created_at_from is not None:
            params["createdAtFrom"] = _format_created_at_from(created_at_from)

        seen_cursors: set[str] = set()
        for _ in range(_MAX_PAGES):
            payload = await self._request("GET", "/v2/transactions", params=params)
            results = list(payload.get("results") or []) if isinstance(payload, dict) else []
            if results:
                yield results

            next_link = payload.get("next") if isinstance(payload, dict) else None
            if not next_link:
                return

            cursor = _extract_after(str(next_link))
            if cursor is None or cursor in seen_cursors:
                logger.warning(
                    "cursor de paginação ausente ou repetido em /v2/transactions (conta %s); "
                    "interrompendo para não girar em laço",
                    account_id,
                )
                return
            seen_cursors.add(cursor)
            params = {**params, "after": cursor}

        logger.warning(
            "limite de %s páginas atingido na conta %s; pode haver transação não lida",
            _MAX_PAGES,
            account_id,
        )


def _safe_body(response: httpx.Response) -> Any:
    """Corpo do erro para diagnóstico, tolerante a resposta que não é JSON."""
    try:
        return _loads(response.content)
    except (ValueError, UnicodeDecodeError):
        return response.text[:500]


# Campos do corpo de erro de `/auth` que podem ir para log e para a tela.
#
# Lista de permissão, não de bloqueio: o corpo de um erro de autenticação pode
# ecoar o que foi enviado, e enumerar o que é seguro é a única forma de garantir
# que o `clientSecret` nunca vaze junto com o diagnóstico.
_SAFE_AUTH_ERROR_FIELDS = ("code", "codeDescription", "message", "errorId")


def _auth_error_hint(response: httpx.Response) -> str:
    """Extrai o motivo da recusa sem arrastar o corpo inteiro junto.

    Existe porque "credenciais recusadas" e "Client is disabled /
    CLIENT_DISABLED" mandam procurar em lugares completamente diferentes — o
    segundo aponta para o estado da Application no Dashboard, não para o `.env`.
    Perder essa distinção custa uma hora de depuração no lugar errado.
    """
    body = _safe_body(response)
    if not isinstance(body, dict):
        return ""
    parts = [f"{k}={body[k]!r}" for k in _SAFE_AUTH_ERROR_FIELDS if k in body]
    return f": {', '.join(parts)}" if parts else ""
