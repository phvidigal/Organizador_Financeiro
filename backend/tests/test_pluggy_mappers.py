"""Mapeamento Pluggy → colunas — sem rede, sem banco, sem event loop.

Módulo síncrono de propósito: os outros carregam o marcador de asyncio no topo, e
um teste síncrono ali dentro vira warning.

Os payloads chegam como JSON cru e passam pelo mesmo parse do cliente. Testar a
partir de dicts Python literais esconderia a armadilha que mais importa aqui — o
`float` que o `json.loads` padrão criaria para `amount`.
"""

import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.models.enums import AccountType, CategorizationStatus, ConnectionStatus, TransactionSource
from app.services.pluggy import mappers
from app.services.pluggy.client import _loads
from app.services.pluggy.mappers import (
    ACCOUNT_ROW_KEYS,
    TRANSACTION_ROW_KEYS,
    map_account,
    map_item,
    map_transaction,
    normalize_amount,
    parse_pluggy_date,
)
from tests.pluggy_fixtures import (
    ACCOUNTS_JSON,
    BANK_ACCOUNT_ID,
    CREDIT_ACCOUNT_ID,
    ITEM_JSON,
    TRANSACTIONS_CREDIT_JSON,
    TRANSACTIONS_PAGE_1_JSON,
    TRANSACTIONS_PAGE_2_JSON,
)

TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
CONNECTION = uuid.UUID("00000000-0000-0000-0000-0000000000cc")


def bank_rows() -> list[dict]:
    """As três transações da conta corrente, na ordem em que a Pluggy as devolve."""
    payload = _loads(TRANSACTIONS_PAGE_1_JSON.encode())
    payload2 = _loads(TRANSACTIONS_PAGE_2_JSON.encode())
    return [
        map_transaction(
            tx, tenant_id=TENANT, account_id=BANK_ACCOUNT_ID, account_type=AccountType.BANK
        )
        for tx in [*payload["results"], *payload2["results"]]
    ]


# --- O contrato com upsert_external_transactions ----------------------------


def test_every_row_of_the_batch_has_the_same_keys() -> None:
    """A armadilha central do lote.

    `upsert_external_transactions` decide o que o `ON CONFLICT` atualiza olhando
    `set(rows[0].keys())` — só a primeira linha. Se o mapeador omitisse chave onde
    não há dado, um lote cuja primeira linha é a mais completa passaria verde e o
    re-sync deixaria de corrigir `merchant` em silêncio.

    A segunda e a terceira transação da fixture são propositalmente incompletas
    (sem `merchant`, sem `categoryId`, sem `descriptionRaw`).
    """
    rows = bank_rows()
    assert len(rows) == 3
    for row in rows:
        assert row.keys() == TRANSACTION_ROW_KEYS

    # E as ausências viraram None, não chave faltando.
    assert rows[1]["merchant"] is None
    assert rows[1]["pluggy_category_id"] is None


def test_kind_is_left_to_ingestion() -> None:
    """`upsert_external_transactions` deriva `kind` do sinal para todas as linhas
    antes de inspecionar as chaves. Preenchê-lo aqui criaria uma segunda fonte da
    mesma regra, que divergiria na primeira vez que uma delas mudasse."""
    assert "kind" not in TRANSACTION_ROW_KEYS


def test_pluggy_guess_is_stored_but_not_adopted() -> None:
    """A Fase 2 guarda o palpite da Pluggy e deixa a transação na fila.

    Adotar a categoria aqui esvaziaria o `ix_transactions_pending` e destruiria a
    régua que a Fase 3 usa para medir se a categorização nativa bastaria.
    """
    row = bank_rows()[0]
    assert row["pluggy_category_id"] == "05000000"
    assert row["pluggy_category_name"] == "Groceries"
    assert row["categorization_status"] == CategorizationStatus.PENDING
    assert "category_id" not in row
    assert "category_source" not in row


# --- Dinheiro ----------------------------------------------------------------


def test_amount_is_decimal_and_exact() -> None:
    """Se o parse passasse por float, `12.34` viraria 12.339999999999999857891452848.

    A comparação com `Decimal("-12.34")` é exata; com float ela falharia.
    """
    row = bank_rows()[0]
    assert isinstance(row["amount"], Decimal)
    assert row["amount"] == Decimal("-12.34")
    assert str(row["amount"]) == "-12.34"


def test_raw_payload_survives_json_dumps() -> None:
    """`parse_float=Decimal` resolve a precisão e cria este problema: `json.dumps`,
    que o SQLAlchemy usa para gravar JSONB, não serializa `Decimal`.

    Sem `jsonable()` o sync morre com `TypeError` na primeira gravação — dentro de
    uma task de background, onde o traceback pode nem chegar ao log.
    """
    for row in bank_rows():
        json.dumps(row["raw_payload"])  # não pode levantar
        json.dumps(row["merchant"])

    # E a precisão sobreviveu à ida para o JSONB.
    assert bank_rows()[0]["raw_payload"]["amount"] == "12.34"


def test_sign_convention_negative_is_always_outflow() -> None:
    rows = bank_rows()
    assert rows[0]["amount"] == Decimal("-12.34")  # DEBIT
    assert rows[1]["amount"] == Decimal("1500.00")  # CREDIT


def test_credit_account_follows_the_same_convention_by_default() -> None:
    """Compra no cartão é saída. ⚠️ Alguns connectors invertem — a confirmação com
    o cartão real é o que decide se `CREDIT_SIGN` muda."""
    payload = _loads(TRANSACTIONS_CREDIT_JSON.encode())
    row = map_transaction(
        payload["results"][0],
        tenant_id=TENANT,
        account_id=CREDIT_ACCOUNT_ID,
        account_type=AccountType.CREDIT,
    )
    assert row["amount"] == Decimal("-89.90")


def test_inverted_credit_sign_flips_only_credit_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    """O interruptor que conserta a inversão sem migração de dados.

    `amount` está em `_SYNCABLE_COLUMNS` de `ingestion.py`, então trocar a constante
    e rodar um sync completo reescreve o histórico inteiro.
    """
    monkeypatch.setattr(mappers, "CREDIT_SIGN", "INVERTED")

    assert normalize_amount(Decimal("89.90"), "DEBIT", AccountType.CREDIT) == Decimal("89.90")
    # Conta corrente não é afetada.
    assert normalize_amount(Decimal("89.90"), "DEBIT", AccountType.BANK) == Decimal("-89.90")


def test_amount_without_type_keeps_the_reported_sign() -> None:
    """Connector que não informa `type`: inventar seria pior que repetir a origem."""
    assert normalize_amount(Decimal("-5.00"), None, AccountType.BANK) == Decimal("-5.00")
    assert normalize_amount(Decimal("5.00"), None, AccountType.BANK) == Decimal("5.00")


# --- Datas -------------------------------------------------------------------


def test_date_is_not_converted_to_local_timezone() -> None:
    """Meia-noite UTC convertida para America/Sao_Paulo retrocede um dia — e na
    virada do mês, um mês. O total de agosto passaria a incluir compra de setembro."""
    assert parse_pluggy_date("2026-08-01T00:00:00.000Z") == date(2026, 8, 1)
    assert parse_pluggy_date("2026-09-01T00:00:00.000Z") == date(2026, 9, 1)

    assert bank_rows()[0]["date"] == date(2026, 8, 1)


def test_pending_transaction_has_no_posted_at() -> None:
    """`posted_at` responde "já compensou?". A terceira transação está PENDING."""
    rows = bank_rows()
    assert rows[0]["posted_at"] is not None
    assert rows[2]["posted_at"] is None
    # O status cru continua disponível para quem quiser o detalhe.
    assert rows[2]["raw_payload"]["status"] == "PENDING"


def test_description_falls_back_when_the_connector_omits_the_raw_form() -> None:
    """A coluna é NOT NULL e é o insumo do LLM. A segunda transação vem sem
    `descriptionRaw`."""
    rows = bank_rows()
    assert rows[0]["description_raw"] == "PADARIA DO ZE LTDA  SAO PAULO BR"
    assert rows[1]["description_raw"] == "PIX RECEBIDO"


def test_source_is_pluggy() -> None:
    assert bank_rows()[0]["source"] == TransactionSource.PLUGGY


def test_long_category_name_is_truncated_to_the_column() -> None:
    """Valor maior que a coluna aborta o INSERT do lote inteiro, não só da linha."""
    tx = {
        "id": "x",
        "amount": Decimal("-1.00"),
        "date": "2026-08-01T00:00:00.000Z",
        "description": "x",
        "category": "C" * 200,
        "categoryId": "I" * 60,
    }
    row = map_transaction(
        tx, tenant_id=TENANT, account_id=BANK_ACCOUNT_ID, account_type=AccountType.BANK
    )
    assert len(row["pluggy_category_name"]) == 120
    assert len(row["pluggy_category_id"]) == 32


# --- Item --------------------------------------------------------------------


def test_map_item_reads_the_connector_block() -> None:
    row = map_item(_loads(ITEM_JSON.encode()))
    assert row["connector_id"] == 201
    assert row["connector_name"] == "Banco de Teste"
    assert row["status"] == ConnectionStatus.UPDATED
    assert row["execution_status"] == "SUCCESS"
    assert row["products"] == ["ACCOUNTS", "CREDIT_CARDS", "TRANSACTIONS"]
    assert row["consent_expires_at"] is not None
    assert row["error"] is None
    # 🔬 No tier pessoal não dá para pedir atualização, então esta é a única
    # resposta honesta para "quando vou ver lançamento novo?".
    assert row["next_auto_sync_at"] == datetime(2026, 8, 11, 9, 0, 0, tzinfo=UTC)


def test_missing_next_auto_sync_is_none_not_invented() -> None:
    """Nem todo connector promete próxima sincronização."""
    item = _loads(ITEM_JSON.encode())
    del item["nextAutoSyncAt"]

    assert map_item(item)["next_auto_sync_at"] is None


def test_unknown_item_status_degrades_instead_of_breaking_the_write() -> None:
    """Um valor novo na enumeração da Pluggy violaria o CHECK e derrubaria o sync
    inteiro. Registrar ERROR preservando o original deixa o motivo à vista."""
    item = _loads(ITEM_JSON.encode())
    item["status"] = "MERGING"

    row = map_item(item)
    assert row["status"] == ConnectionStatus.ERROR
    assert row["error"] == {"unmapped_status": "MERGING"}


def test_item_error_is_preserved_raw() -> None:
    item = _loads(ITEM_JSON.encode())
    item["error"] = {"code": "INVALID_CREDENTIALS", "message": "senha incorreta"}
    item["status"] = "LOGIN_ERROR"

    row = map_item(item)
    assert row["status"] == ConnectionStatus.LOGIN_ERROR
    assert row["error"]["code"] == "INVALID_CREDENTIALS"


# --- Contas ------------------------------------------------------------------


def accounts() -> list[dict]:
    payload = _loads(ACCOUNTS_JSON.encode())
    return [
        map_account(acc, tenant_id=TENANT, bank_connection_id=CONNECTION)
        for acc in payload["results"]
    ]


def test_account_rows_are_uniform_and_typed() -> None:
    rows = accounts()
    for row in rows:
        assert row.keys() == ACCOUNT_ROW_KEYS

    checking, card = rows
    assert checking["type"] == AccountType.BANK
    assert checking["subtype"] == "CHECKING_ACCOUNT"
    assert checking["pluggy_account_id"] == BANK_ACCOUNT_ID
    assert checking["balance"] == Decimal("2500.75")
    assert card["type"] == AccountType.CREDIT
    assert card["pluggy_account_id"] == CREDIT_ACCOUNT_ID
    assert card["marketing_name"] is None


def test_personal_data_is_not_copied() -> None:
    """`taxNumber` (CPF) e `owner` vêm na resposta e não são persistidos.

    Minimização é princípio da LGPD (art. 6º, III): dado pessoal sem uso no MVP não
    deve ser coletado. Se virarem necessários, entram criptografados na Fase 5.
    """
    for row in accounts():
        assert "tax_number" not in row
        assert "owner" not in row
        blob = json.dumps({k: v for k, v in row.items() if isinstance(v, dict | list)})
        assert "123.456.789-00" not in blob
        assert "Fulano" not in blob


def test_unknown_account_subtype_becomes_null() -> None:
    """Nullable, então dá para degradar; o valor original sobrevive no bloco cru."""
    payload = _loads(ACCOUNTS_JSON.encode())
    account = payload["results"][0]
    account["subtype"] = "INVESTMENT_ACCOUNT"

    row = map_account(account, tenant_id=TENANT, bank_connection_id=CONNECTION)
    assert row["subtype"] is None


def test_unknown_account_type_fails_loudly() -> None:
    """`type` é NOT NULL e alimenta a normalização de sinal do cartão. Gravar um
    valor errado faria conta corrente ser tratada como crédito — falhar é melhor."""
    payload = _loads(ACCOUNTS_JSON.encode())
    account = payload["results"][0]
    account["type"] = "INVESTMENT"

    with pytest.raises(ValueError, match="tipo de conta desconhecido"):
        map_account(account, tenant_id=TENANT, bank_connection_id=CONNECTION)
