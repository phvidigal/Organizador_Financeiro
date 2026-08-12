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

Some quando a validação terminar — ou fica, guardado pelo bloqueio de produção,
como ferramenta de depuração de connector novo.
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
from app.services.pluggy.mappers import jsonable, map_account, map_item, map_transaction

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pluggy", tags=["pluggy-diagnostics"])

# UUIDs de fachada: o diagnóstico não grava nada, mas os mapeadores pedem os
# identificadores para montar a linha que *seria* gravada.
_FAKE_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
_FAKE_CONNECTION = uuid.UUID("00000000-0000-0000-0000-0000000000ff")


def _require_non_production(settings: Settings) -> None:
    if settings.environment == "production":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


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
    """Dump cru da Pluggy para o item informado, sem tocar no banco."""
    _require_non_production(settings)

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

    result["item"] = jsonable(item)
    result["item_interpretado"] = jsonable(map_item(item))

    accounts = await client.list_accounts(item_id)
    result["accounts"] = jsonable(accounts)

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

        # (iii) Sinal do cartão: `bruto` é o que a Pluggy mandou, `gravaria` é o que
        # a normalização produziria. Uma compra conhecida do cartão resolve a dúvida.
        amostra = []
        account_id = uuid.UUID(str(account["id"]))
        async for page in client.iter_transactions(account_id):
            for tx in page[:3]:
                row = map_transaction(
                    tx,
                    tenant_id=_FAKE_TENANT,
                    account_id=account_id,
                    account_type=account.get("type"),
                )
                amostra.append(
                    {
                        "bruto": jsonable(tx),
                        "gravaria": {
                            "amount": str(row["amount"]),
                            "date": row["date"].isoformat(),
                            "posted_at": row["posted_at"].isoformat()
                            if row["posted_at"]
                            else None,
                            "pluggy_category_id": row["pluggy_category_id"],
                            "pluggy_category_name": row["pluggy_category_name"],
                        },
                    }
                )
            break  # só a primeira página; isto é diagnóstico, não sync
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
