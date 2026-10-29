"""Endpoint de diagnóstico da Pluggy — temporário, para a validação da Fase 2.

Existe para responder, com dado real e antes de o sync estar escrito, três coisas
que `docs/pluggy-api.md` marca como não confirmadas:

1. **shape do `POST /auth`** — o campo da chave é `apiKey`? `accessToken`?
2. **`PATCH /items/{id}` funciona no tier gratuito?** Se não, o sync vira só
   leitura e a interface não pode prometer "atualizar agora".
3. **sinal em conta de crédito** — alguns connectors reportam compra no cartão como
   valor positivo. O bloco `interpretado` de cada transação mostra lado a lado o
   que a Pluggy mandou e o que gravaríamos.

Não escreve nada no banco e não devolve segredo: da resposta de `/auth` saem
apenas os *nomes* dos campos, porque um dos valores é a própria chave.

**Desligado por padrão** (`ENABLE_PLUGGY_DIAGNOSTICS=false` → 404). Liga-se para
depurar um connector novo e desliga-se em seguida. O bloqueio antigo era só
"fora de produção" — e o compose sempre roda em desenvolvimento, então o endpoint
ficava aberto o tempo todo.

**E sem dado pessoal, mesmo ligado.** A resposta crua de `/accounts` traz o CPF
(`taxNumber`) e o nome (`owner`) do titular, e a de transações traz `paymentData`
de terceiros. O dump passa pelos mesmos filtros da gravação: o que o banco não
guarda, a tela também não mostra.
"""

import logging
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_pluggy_client
from app.core.config import Settings, get_settings
from app.services.pluggy.client import _loads
from app.services.pluggy.errors import PluggyError
from app.services.pluggy.mappers import (
    counterparty_is_holder,
    holder_identity,
    jsonable,
    map_account,
    map_item,
    map_transaction,
    minimize_raw_payload,
)

logger = logging.getLogger(__name__)


def _require_enabled(settings: Settings = Depends(get_settings)) -> None:
    """404, e não 403: desligado, o endpoint não existe para quem pergunta."""
    if not settings.enable_pluggy_diagnostics:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


# Dependência do router, e não do handler: o FastAPI a resolve antes das do
# endpoint. Sem isso, `get_pluggy_client` responderia 503 por falta de credencial
# antes de o 404 ter chance — e anunciaria que o endpoint existe.
router = APIRouter(
    prefix="/pluggy",
    tags=["pluggy-diagnostics"],
    dependencies=[Depends(_require_enabled)],
)

# Campos de `/accounts` que identificam o titular. Os mesmos que `map_account` não
# copia para o banco.
_ACCOUNT_PERSONAL_FIELDS = frozenset({"taxNumber", "owner"})
# `clientUserId` é o identificador que quem criou o item escolheu — no Meu Pluggy,
# pode ser o e-mail ou o CPF.
_ITEM_PERSONAL_FIELDS = frozenset({"clientUserId"})


def _without(payload: dict[str, Any], fields: frozenset[str]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in fields}

# UUIDs de fachada: o diagnóstico não grava nada, mas os mapeadores pedem os
# identificadores para montar a linha que *seria* gravada.
_FAKE_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
_FAKE_CONNECTION = uuid.UUID("00000000-0000-0000-0000-0000000000ff")


async def _probe_auth(settings: Settings) -> dict[str, Any]:
    """Chama `POST /auth` cru e devolve só a *forma* da resposta.

    Nunca os valores: um deles é a API key, e esta resposta vai para a tela e
    possivelmente para um print colado num chat.
    """
    url = f"{settings.pluggy_base_url.rstrip('/')}/auth"
    payload = {
        "clientId": settings.pluggy_client_id,
        "clientSecret": settings.pluggy_client_secret,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            response = await http.post(url, json=payload)
    except Exception as exc:
        return {"status_code": None, "error": f"{type(exc).__name__}: {exc}"}

    result: dict[str, Any] = {"status_code": response.status_code}
    try:
        body = _loads(response.content)
    except Exception:
        result["campos"] = "resposta não é JSON"
        return result

    if isinstance(body, dict):
        result["campos"] = sorted(body)
        # O tamanho ajuda a distinguir um JWT de um token opaco sem revelá-lo.
        result["tamanhos"] = {k: len(v) for k, v in body.items() if isinstance(v, str)}
    else:
        result["campos"] = type(body).__name__
    return result


@router.get("/diagnostics")
async def diagnostics(
    item_id: uuid.UUID = Query(..., description="itemId de uma conexão existente na Pluggy"),
    refresh: bool = Query(False, description="tentar PATCH /items para medir o tier gratuito"),
    settings: Settings = Depends(get_settings),
    client=Depends(get_pluggy_client),
) -> dict[str, Any]:
    """Dump da Pluggy para o item informado, sem tocar no banco e sem dado pessoal."""
    result: dict[str, Any] = {"item_id": str(item_id), "auth": await _probe_auth(settings)}

    # (ii) O tier gratuito deixa disparar atualização de item?
    if refresh:
        try:
            await client.refresh_item(item_id)
            result["patch_items"] = {"permitido": True}
        except PluggyError as exc:
            result["patch_items"] = {
                "permitido": False,
                "erro": type(exc).__name__,
                "detalhe": str(exc)[:500],
            }

    try:
        item = await client.get_item(item_id)
    except PluggyError as exc:
        result["erro"] = f"{type(exc).__name__}: {exc}"
        return result

    result["item"] = jsonable(_without(item, _ITEM_PERSONAL_FIELDS))
    result["item_interpretado"] = jsonable(map_item(item))

    accounts = await client.list_accounts(item_id)
    result["accounts"] = [
        jsonable(_without(account, _ACCOUNT_PERSONAL_FIELDS)) for account in accounts
    ]

    # Se a detecção de conta própria tem com o que trabalhar. Só contagens: o CPF e
    # o nome do titular não saem daqui, nem mascarados.
    holder = holder_identity(accounts)
    result["titular"] = {
        "documentos": len(holder.documents),
        "nomes": len(holder.names),
    }

    contas: list[dict[str, Any]] = []
    for account in accounts:
        entrada: dict[str, Any] = {"id": account.get("id"), "type": account.get("type")}
        try:
            entrada["interpretado"] = jsonable(
                map_account(
                    account, tenant_id=_FAKE_TENANT, bank_connection_id=_FAKE_CONNECTION
                )
            )
        except ValueError as exc:
            entrada["interpretado"] = f"não mapeável: {exc}"

        # (iii) Sinal do cartão: `bruto` é o que a Pluggy mandou (sem dado pessoal,
        # mas com `amount` e `type` intactos), `gravaria` é o que a normalização
        # produziria. Uma compra conhecida do cartão resolve a dúvida.
        amostra = []
        account_id = uuid.UUID(str(account["id"]))
        # Só a primeira página — isto é diagnóstico, não sync. `aclose` explícito
        # porque abandonar um gerador assíncrono no meio deixa a resposta HTTP dele
        # aberta até o coletor de lixo passar.
        pages = client.iter_transactions(account_id)
        try:
            page = await anext(pages, [])
        finally:
            await pages.aclose()

        # Quanto da primeira página a detecção resolveria. `sem_dado` alto quer dizer
        # que a Pluggy não mandou `paymentData` — aí a evidência não existe, e a
        # conta própria continua dependendo da revisão.
        contraparte = {"titular": 0, "outra": 0, "sem_dado": 0}
        for tx in page:
            match counterparty_is_holder(tx, holder):
                case True:
                    contraparte["titular"] += 1
                case False:
                    contraparte["outra"] += 1
                case None:
                    contraparte["sem_dado"] += 1
        entrada["contraparte"] = contraparte

        for tx in page[:3]:
            row = map_transaction(
                tx,
                tenant_id=_FAKE_TENANT,
                account_id=account_id,
                account_type=account.get("type"),
                holder=holder,
            )
            amostra.append(
                {
                    # O que seria gravado em `raw_payload`, não a resposta
                    # inteira: `paymentData` traz nome e CPF de terceiros.
                    "bruto": minimize_raw_payload(tx),
                    "gravaria": {
                        "amount": str(row["amount"]),
                        "date": row["date"].isoformat(),
                        "posted_at": row["posted_at"].isoformat()
                        if row["posted_at"]
                        else None,
                        "pluggy_category_id": row["pluggy_category_id"],
                        "pluggy_category_name": row["pluggy_category_name"],
                        "counterparty_is_holder": row["counterparty_is_holder"],
                    },
                }
            )
        entrada["transacoes"] = amostra
        contas.append(entrada)

    result["contas"] = contas

    # Insumo para escrever PLUGGY_TO_LOCAL em category_map.py.
    categories = await client.list_categories()
    result["categories"] = jsonable(categories)
    result["categories_resumo"] = sorted(
        f"{c.get('id')} · {c.get('description')} · {c.get('descriptionTranslated')}"
        for c in categories
    )

    return result
