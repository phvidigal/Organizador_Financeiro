"""Tradução do JSON da Pluggy para dicts de coluna.

Funções puras: sem `httpx`, sem `AsyncSession`, sem relógio. É o que torna estes
mapeamentos testáveis sem rede e sem banco — e é o que mantém a convenção de sinal
do cartão de crédito (`CREDIT_SIGN`) num único lugar sem I/O, para ser corrigida
com a troca de uma constante quando o dado real mostrar como o connector se
comporta.

As saídas são dicts cujas chaves são nomes de coluna dos modelos SQLAlchemy — é o
que `app/services/ingestion.py` e `app/services/accounts.py` consomem.
"""

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from app.core.text import fold
from app.models.enums import (
    AccountSubtype,
    AccountType,
    CategorizationStatus,
    ConnectionStatus,
    TransactionSource,
)

# Convenção de sinal em conta de crédito.
#
# ⚠️ Alguns connectors reportam compra no cartão como valor POSITIVO, invertendo a
# convenção do restante. Até haver confirmação com o cartão real, o padrão é
# confiar no que a Pluggy manda.
#
# Se o dado real mostrar inversão: troque para "INVERTED" e rode um sync completo
# (`POST /connections/{id}/sync?full=true`). `amount` está em `_SYNCABLE_COLUMNS` de
# `ingestion.py`, então o upsert reescreve todas as linhas — sem migração de dados,
# sem script de correção. É exatamente por isso que esta constante existe num único
# lugar, em vez de um `if` espalhado pelo sync.
CREDIT_SIGN: Literal["AS_REPORTED", "INVERTED"] = "AS_REPORTED"


# Contrato com `upsert_external_transactions`: TODA linha do lote precisa ter
# exatamente estas chaves. Ver a docstring de `map_transaction`.
TRANSACTION_ROW_KEYS = frozenset(
    {
        "tenant_id",
        "account_id",
        "source",
        "external_id",
        "amount",
        "currency_code",
        "date",
        "posted_at",
        "description_raw",
        "description_clean",
        "merchant",
        "pluggy_category_id",
        "pluggy_category_name",
        "categorization_status",
        "raw_payload",
        "counterparty_is_holder",
    }
)

ACCOUNT_ROW_KEYS = frozenset(
    {
        "tenant_id",
        "bank_connection_id",
        "pluggy_account_id",
        "type",
        "subtype",
        "name",
        "marketing_name",
        "number",
        "balance",
        "currency_code",
        "bank_data",
        "credit_data",
    }
)


# --- raw_payload --------------------------------------------------------------
#
# O que da resposta crua de uma transação fica guardado em `raw_payload`.
#
# **Lista de permissão, não de bloqueio** — mesmo critério de
# `client._SAFE_AUTH_ERROR_FIELDS`. A resposta da Pluggy traz, em `paymentData`,
# nome, CPF/CNPJ, agência e conta de **terceiros**: quem mandou o Pix, quem o
# recebeu. Nada disso é necessário para reprocessar a transação, e minimização é
# princípio da LGPD (art. 6º, III). Enumerar o que é seguro é a única forma de
# garantir que um campo novo da Pluggy não comece a ser gravado em silêncio: o que
# não está aqui cai fora, e entrar exige decidir.
#
# A migration `0006_minimize_raw_payload` aplicou esta mesma lista às linhas já
# gravadas, numa cópia congelada. Mudou a lista? A migration não muda junto — ela
# descreve o que foi feito naquele dia.
RAW_TRANSACTION_FIELDS = frozenset(
    {
        "id",
        "accountId",
        "description",
        "descriptionRaw",
        "amount",
        "amountInAccountCurrency",
        "currencyCode",
        "date",
        "type",
        "status",
        "category",
        "categoryId",
        "balance",
        "operationType",
        "providerCode",
        "providerId",
        "createdAt",
        "updatedAt",
        # Empresa (nome, CNPJ, CNAE): dado público de pessoa jurídica.
        "merchant",
    }
)

# Parcelamento e MCC servem para categorizar; o número do cartão, não.
RAW_CREDIT_CARD_FIELDS = frozenset(
    {"installmentNumber", "totalInstallments", "totalAmount", "payeeMCC", "purchaseDate", "billId"}
)


def _document_type(party: Any) -> str | None:
    """`"CPF"` ou `"CNPJ"` de quem pagou ou recebeu — o tipo, nunca o número."""
    if not isinstance(party, dict):
        return None
    document = party.get("documentNumber")
    if not isinstance(document, dict):
        return None
    kind = document.get("type")
    return kind if isinstance(kind, str) else None


def minimize_raw_payload(tx: dict[str, Any]) -> dict[str, Any]:
    """A transação crua da Pluggy, sem dado pessoal de terceiros. Pura.

    De `paymentData` sobram só o meio (`paymentMethod`: PIX, TED, boleto) e o
    **tipo** de documento de cada ponta. Este último é sinal de categorização que
    vale guardar — Pix vindo de CNPJ tende a ser pagamento por serviço, vindo de CPF
    pode ser qualquer coisa — e não identifica ninguém.

    Limite que esta função não resolve: `descriptionRaw` costuma trazer o nome do
    remetente ("PIX RECEBIDO FULANO"), e é o insumo do LLM. Fica.
    """
    raw: dict[str, Any] = {key: value for key, value in tx.items() if key in RAW_TRANSACTION_FIELDS}

    payment = tx.get("paymentData")
    if isinstance(payment, dict):
        kept: dict[str, Any] = {}
        if isinstance(payment.get("paymentMethod"), str):
            kept["paymentMethod"] = payment["paymentMethod"]
        for party in ("payer", "receiver"):
            document_type = _document_type(payment.get(party))
            if document_type is not None:
                kept[party] = {"documentNumber": {"type": document_type}}
        raw["paymentData"] = kept

    card = tx.get("creditCardMetadata")
    if isinstance(card, dict):
        raw["creditCardMetadata"] = {
            key: value for key, value in card.items() if key in RAW_CREDIT_CARD_FIELDS
        }

    return jsonable(raw)


# --- O titular, e se o outro lado de um Pix é ele ----------------------------
#
# É o que tira a transferência entre contas próprias dos totais **sem palpite**: a
# Pluggy manda, em `paymentData`, o CPF e o nome de quem pagou e de quem recebeu, e
# as contas do item trazem o CPF (`taxNumber`) e o nome (`owner`) do titular. Se a
# ponta do outro lado é ele, o dinheiro não saiu de casa.
#
# **A comparação acontece em memória, durante o sync, e só o resultado é gravado**
# (`transactions.counterparty_is_holder`). O documento de ninguém chega ao banco —
# nem o do titular, que `map_account` também não copia.

_NON_DIGITS = re.compile(r"\D")


def _digits(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    digits = _NON_DIGITS.sub("", value)
    return digits or None


# Nome sem acento e em caixa alta: a mesma pessoa chega com e sem acento conforme
# o banco do outro lado, e uma diferença de `É` para `E` não pode transformar
# transferência própria em receita.
normalize_person_name = fold


@dataclass(frozen=True, repr=False)
class HolderIdentity:
    """CPF(s) e nome(s) do titular, tirados das contas do item. Vive só em memória.

    `repr` mostra só as contagens: um `logger.exception` com esta instância no
    traceback não pode despejar o documento no log.
    """

    documents: frozenset[str] = frozenset()
    names: frozenset[str] = frozenset()

    def __repr__(self) -> str:
        return f"HolderIdentity(documents={len(self.documents)}, names={len(self.names)})"

    @property
    def known(self) -> bool:
        return bool(self.documents or self.names)


def holder_identity(accounts: Iterable[dict[str, Any]]) -> HolderIdentity:
    """O titular a partir das contas cruas da Pluggy (`taxNumber` e `owner`)."""
    documents: set[str] = set()
    names: set[str] = set()
    for account in accounts:
        if not isinstance(account, dict):
            continue
        document = _digits(account.get("taxNumber"))
        if document:
            documents.add(document)
        name = normalize_person_name(account.get("owner"))
        if name:
            names.add(name)
    return HolderIdentity(documents=frozenset(documents), names=frozenset(names))


def counterparty_is_holder(tx: dict[str, Any], holder: HolderIdentity | None) -> bool | None:
    """O outro lado da transação é o próprio titular? Pura.

    A ponta que importa depende da direção: numa saída, quem **recebeu**; numa
    entrada, quem **pagou**. Casa primeiro pelo documento e, se ele faltar, pelo
    nome normalizado — exato, sem aproximação: um falso positivo tira gasto real
    dos totais, que é o erro mais caro.

    `None` é "não sei" — sem titular conhecido, sem `paymentData`, ou ponta sem
    documento nem nome — e não pode ser lido como "não é".
    """
    if holder is None or not holder.known:
        return None
    payment = tx.get("paymentData")
    if not isinstance(payment, dict):
        return None

    direction = tx.get("type")
    if direction not in ("DEBIT", "CREDIT"):
        amount = as_decimal(tx.get("amount"))
        if amount is None or amount == 0:
            return None
        direction = "DEBIT" if amount < 0 else "CREDIT"
    party = payment.get("receiver" if direction == "DEBIT" else "payer")
    if not isinstance(party, dict):
        return None

    document_block = party.get("documentNumber")
    document = _digits(document_block.get("value")) if isinstance(document_block, dict) else None
    name = normalize_person_name(party.get("name"))

    if document and document in holder.documents:
        return True
    if name and name in holder.names:
        return True
    if document or name:
        return False
    return None


# --- helpers ----------------------------------------------------------------


def jsonable(value: Any) -> Any:
    """Converte `Decimal` em string para o payload caber num JSONB.

    O parse da resposta usa `parse_float=Decimal` (ver `client._loads`) para que
    nenhum valor monetário passe por float. O efeito colateral é que o payload
    inteiro carrega `Decimal` — e `json.dumps`, que o SQLAlchemy usa ao gravar
    JSONB, levanta `TypeError` neles. Sem esta função o sync morre na primeira
    gravação, dentro de uma task de background.

    String e não float na volta: o objetivo de `raw_payload` é permitir reprocessar
    sem perder precisão, e voltar para float desfaria exatamente o que o parse
    cuidadoso conquistou.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    return value


def as_decimal(value: Any) -> Decimal | None:
    """Converte para `Decimal` sem nunca passar por float.

    `float` só aparece aqui se alguém parsear o JSON sem `parse_float=Decimal`; a
    conversão via `str` limita o estrago ao que já se perdeu, em vez de propagar
    binário para o banco.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool é subclasse de int; não é dinheiro
        return None
    if isinstance(value, int):
        return Decimal(value)
    return Decimal(str(value))


def parse_pluggy_date(value: str) -> date_type:
    """Data do lançamento, **sem conversão de fuso**.

    A Pluggy devolve meia-noite UTC (`"2026-08-01T00:00:00.000Z"`). Converter para
    `America/Sao_Paulo` jogaria o lançamento para o dia anterior e, na virada do
    mês, para o mês anterior — o total de agosto passaria a incluir uma compra de
    setembro. A parte da data da string é o que a instituição reportou; é ela que
    vale.
    """
    return date_type.fromisoformat(value[:10])


def parse_pluggy_datetime(value: str | None) -> datetime | None:
    """Timestamp completo, tolerante ao `Z` que o `fromisoformat` antigo recusava."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_amount(
    raw_amount: Any, tx_type: str | None, account_type: str | None
) -> Decimal:
    """Convenção do banco: negativo = saída, sempre.

    Único lugar do projeto onde um valor vindo da Pluggy muda de sinal. Concentrar
    isso aqui é o que permite corrigir a convenção do cartão trocando uma constante.
    """
    amount = as_decimal(raw_amount)
    if amount is None:
        raise ValueError("transação da Pluggy sem `amount`")

    if tx_type == "DEBIT":
        amount = -abs(amount)
    elif tx_type == "CREDIT":
        amount = abs(amount)
    # Sem `type`, confia no sinal recebido: inventar um seria pior que repetir o
    # que a origem disse, e `raw_payload` guarda o original para reprocessar.

    if account_type == AccountType.CREDIT and CREDIT_SIGN == "INVERTED":
        amount = -amount

    return amount


def _truncate(value: Any, limit: int) -> str | None:
    """Corta no tamanho da coluna. Valor longo demais aborta o INSERT do lote inteiro."""
    if value is None:
        return None
    text = str(value)
    return text[:limit] if text else None


def _coerce_status(raw: Any) -> tuple[str, dict[str, Any] | None]:
    """Encaixa o `status` do item no CHECK de `bank_connections`.

    Um valor novo na enumeração da Pluggy derrubaria a gravação e levaria o sync
    inteiro junto. Registrar `ERROR` preservando o valor original é menos ruim: a
    conexão aparece como problemática na tela, com o motivo real à vista, em vez de
    o sync falhar sem explicação.
    """
    if isinstance(raw, str) and raw in ConnectionStatus.__members__:
        return raw, None
    return ConnectionStatus.ERROR.value, {"unmapped_status": raw}


# --- mapeadores --------------------------------------------------------------


def map_item(item: dict[str, Any]) -> dict[str, Any]:
    """Item da Pluggy → colunas de `bank_connections`.

    Não inclui `tenant_id` nem `pluggy_item_id`: essas identificam a linha e são
    responsabilidade de quem grava.
    """
    status, status_error = _coerce_status(item.get("status"))

    error: dict[str, Any] | None = None
    raw_error = item.get("error")
    if isinstance(raw_error, dict):
        error = jsonable(raw_error)
    if status_error is not None:
        error = {**(error or {}), **status_error}

    connector = item.get("connector") or {}
    products = item.get("products")

    return {
        "connector_id": connector.get("id") if isinstance(connector, dict) else None,
        "connector_name": _truncate(
            connector.get("name") if isinstance(connector, dict) else None, 120
        ),
        "status": status,
        # Texto livre de propósito: a Pluggy documenta `executionStatus` de forma
        # menos estável que `status`, e não há CHECK nesta coluna para violar.
        "execution_status": _truncate(item.get("executionStatus"), 64),
        "products": [str(p)[:32] for p in products] if isinstance(products, list) else None,
        "consent_expires_at": parse_pluggy_datetime(item.get("consentExpiresAt")),
        # Quando a Pluggy vai buscar dado novo sozinha. É o que a interface mostra
        # no lugar de "atualizar agora", já que o tier pessoal não aceita o PATCH.
        "next_auto_sync_at": parse_pluggy_datetime(item.get("nextAutoSyncAt")),
        "error": error,
    }


def map_account(
    account: dict[str, Any], *, tenant_id: uuid.UUID, bank_connection_id: uuid.UUID
) -> dict[str, Any]:
    """Conta da Pluggy → colunas de `accounts`.

    `taxNumber` (CPF) e `owner` **não são copiados**: minimização é princípio da
    LGPD (art. 6º, III). Ficam de fora inclusive do `bank_data`, que é copiado como
    veio. O único uso deles é em memória, por `holder_identity`, para reconhecer
    transferência entre contas próprias.

    Chaves uniformes pela mesma razão de `map_transaction` — o upsert de contas usa
    a mesma técnica de inspecionar o lote.
    """
    raw_type = account.get("type")
    if raw_type not in AccountType.__members__:
        # `type` é NOT NULL e não há como degradar: gravar um tipo errado faria a
        # normalização de sinal do cartão tratar conta corrente como crédito.
        raise ValueError(f"tipo de conta desconhecido na Pluggy: {raw_type!r}")

    raw_subtype = account.get("subtype")
    # `AccountSubtype` cobre os três casos do MVP; a Pluggy tem outros (investimento,
    # empréstimo). Nullable, então o desconhecido vira NULL e o original sobrevive
    # em `bank_data`/`credit_data`.
    subtype = raw_subtype if raw_subtype in AccountSubtype.__members__ else None

    row = {
        "tenant_id": tenant_id,
        "bank_connection_id": bank_connection_id,
        "pluggy_account_id": uuid.UUID(str(account["id"])),
        "type": raw_type,
        "subtype": subtype,
        "name": _truncate(account.get("name"), 160) or "Conta",
        "marketing_name": _truncate(account.get("marketingName"), 160),
        "number": _truncate(account.get("number"), 64),
        "balance": as_decimal(account.get("balance")),
        "currency_code": (account.get("currencyCode") or "BRL")[:3],
        "bank_data": jsonable(account.get("bankData")),
        "credit_data": jsonable(account.get("creditData")),
    }
    assert row.keys() == ACCOUNT_ROW_KEYS, "map_account mudou de chaves sem atualizar o contrato"
    return row


def _description_raw(tx: dict[str, Any]) -> str:
    """Descrição como a instituição reportou, com fallback.

    A coluna é NOT NULL e é o insumo do LLM da Fase 3. Alguns connectors preenchem
    só `description`; a cadeia cobre isso. String vazia como último recurso é
    honesta — significa que a origem não mandou descrição nenhuma.
    """
    return str(tx.get("descriptionRaw") or tx.get("description") or "")


def map_transaction(
    tx: dict[str, Any],
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    account_type: str | None,
    holder: HolderIdentity | None,
) -> dict[str, Any]:
    """Transação da Pluggy → dict de colunas que `ingestion.py` espera.

    `holder` é obrigatório, mesmo podendo ser `None`: quem chama precisa decidir se
    tem o titular em mãos. Um default silencioso desligaria a detecção de conta
    própria num chamador novo sem ninguém notar.

    **Todas** as chaves de `TRANSACTION_ROW_KEYS` estão sempre presentes, com `None`
    onde não há dado, e nenhum `if` omite chave. Isso não é preciosismo:
    `upsert_external_transactions` decide o que o `ON CONFLICT` atualiza olhando
    `set(rows[0].keys())` — só a primeira linha do lote. Um lote onde a linha 1 não
    traz `merchant` e a linha 3 traz faria `merchant` sumir do UPDATE, e o re-sync
    deixaria de corrigir dado sem erro nenhum; uma chave presente só na linha 3
    quebraria o INSERT na compilação.

    `kind` fica de fora de propósito: `upsert_external_transactions` o deriva do
    sinal para todas as linhas antes de inspecionar as chaves, então incluí-lo aqui
    só criaria uma segunda fonte da mesma regra.

    `category_id` e `category_source` também ficam de fora, e `categorization_status`
    entra como PENDING. A Fase 2 **guarda** o palpite da Pluggy
    (`pluggy_category_id`/`pluggy_category_name`) mas não o adota: adotá-lo
    esvaziaria a fila do `ix_transactions_pending` e destruiria a régua que a Fase 3
    usa para medir se a categorização nativa bastaria.
    """
    status = tx.get("status")
    row = {
        "tenant_id": tenant_id,
        "account_id": account_id,
        "source": TransactionSource.PLUGGY.value,
        "external_id": _truncate(tx["id"], 128),
        "amount": normalize_amount(tx.get("amount"), tx.get("type"), account_type),
        "currency_code": (tx.get("currencyCode") or "BRL")[:3],
        "date": parse_pluggy_date(str(tx["date"])),
        # NULL enquanto a transação não compensou. Dá semântica real à coluna
        # ("já caiu?") sem precisar de coluna nova; o `status` cru fica em
        # `raw_payload` para quem quiser o detalhe.
        "posted_at": None if status == "PENDING" else parse_pluggy_datetime(str(tx["date"])),
        "description_raw": _description_raw(tx),
        "description_clean": _truncate(tx.get("description"), 10_000),
        "merchant": jsonable(tx.get("merchant")),
        "pluggy_category_id": _truncate(tx.get("categoryId"), 32),
        "pluggy_category_name": _truncate(tx.get("category"), 120),
        "categorization_status": CategorizationStatus.PENDING.value,
        # Sem dado pessoal de terceiros; ver `RAW_TRANSACTION_FIELDS`.
        "raw_payload": minimize_raw_payload(tx),
        # Só o booleano: o documento que o decidiu não sai daqui.
        "counterparty_is_holder": counterparty_is_holder(tx, holder),
    }
    assert row.keys() == TRANSACTION_ROW_KEYS, (
        "map_transaction mudou de chaves sem atualizar o contrato — "
        "lote heterogêneo quebra o ON CONFLICT de upsert_external_transactions"
    )
    return row
