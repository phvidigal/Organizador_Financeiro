"""Funções puras do serviço de ingestão — sem banco, sem event loop.

Separadas dos testes de schema de propósito: os outros módulos carregam um
marcador de asyncio no topo, e um teste síncrono ali dentro vira warning."""

import uuid
from datetime import date
from decimal import Decimal

from app.models.enums import TransactionKind
from app.services.ingestion import (
    assign_dedup_seq,
    compute_dedup_hash,
    derive_kind,
    normalize_description,
)


def test_hash_ignores_amount_and_whitespace_formatting() -> None:
    """`12.5` e `12.50` são a mesma quantia; espaçamento diferente é a mesma
    descrição. Se o hash mudasse por causa disso, reimportar o extrato duplicaria
    tudo — que é exatamente o que a constraint deveria impedir."""
    account_id = uuid.uuid4()
    base = compute_dedup_hash(account_id, date(2026, 8, 1), Decimal("-12.50"), "CAFE  CENTRAL")

    assert base == compute_dedup_hash(
        account_id, date(2026, 8, 1), Decimal("-12.5"), " cafe central "
    )
    assert base != compute_dedup_hash(
        account_id, date(2026, 8, 1), Decimal("-12.51"), "CAFE CENTRAL"
    )


def test_hash_separates_accounts_and_dates() -> None:
    """Mesma compra em contas ou dias diferentes são transações diferentes."""
    a, b = uuid.uuid4(), uuid.uuid4()
    args = (Decimal("-12.50"), "CAFE")
    assert compute_dedup_hash(a, date(2026, 8, 1), *args) != compute_dedup_hash(
        b, date(2026, 8, 1), *args
    )
    assert compute_dedup_hash(a, date(2026, 8, 1), *args) != compute_dedup_hash(
        a, date(2026, 8, 2), *args
    )


def test_normalize_description_does_not_touch_the_original() -> None:
    """A normalização serve só ao hash. `description_raw` é o insumo do LLM e da
    futura base de regras, e precisa continuar exatamente como veio da origem."""
    original = "  Compra   no  Café  "
    assert normalize_description(original) == "COMPRA NO CAFÉ"
    assert original == "  Compra   no  Café  "


def test_dedup_seq_numbers_repeats_in_file_order() -> None:
    rows = [
        {"dedup_hash": "aaa"},
        {"dedup_hash": "bbb"},
        {"dedup_hash": "aaa"},
        {"dedup_hash": "aaa"},
    ]
    assert [r["dedup_seq"] for r in assign_dedup_seq(rows)] == [0, 0, 1, 2]


def test_dedup_seq_is_stable_across_reimports() -> None:
    """A mesma entrada produz a mesma numeração — é o que faz a reimportação
    colidir com o que já está gravado em vez de duplicar."""
    rows = [{"dedup_hash": h} for h in ["x", "y", "x", "z", "x"]]
    first = [r["dedup_seq"] for r in assign_dedup_seq(rows)]
    second = [r["dedup_seq"] for r in assign_dedup_seq(rows)]
    assert first == second == [0, 0, 1, 0, 2]


# --- Natureza da transação --------------------------------------------------


def test_kind_is_derived_from_the_sign() -> None:
    assert derive_kind(Decimal("-10.00")) is TransactionKind.EXPENSE
    assert derive_kind(Decimal("2500.00")) is TransactionKind.INCOME
    # Zero como INCOME é arbitrário, mas precisa ser determinístico: estorno de
    # valor cheio aparece assim em alguns connectors.
    assert derive_kind(Decimal("0.00")) is TransactionKind.INCOME


def test_transfer_is_never_derived_automatically() -> None:
    """Nenhuma origem de dado diz sozinha que um lançamento é transferência —
    isso depende de reconhecer que o destino é conta do próprio titular. Até ser
    classificada, a transferência conta como gasto.

    O caminho oposto (assumir TRANSFER e corrigir depois) esconderia gasto real,
    que é o erro mais caro dos dois.
    """
    assert derive_kind(Decimal("-2000.00")) is not TransactionKind.TRANSFER
