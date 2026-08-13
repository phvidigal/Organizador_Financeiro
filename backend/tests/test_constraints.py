"""Deduplicação de transações.

O que estes testes protegem é a promessa central do schema: re-sincronizar ou
reimportar não pode duplicar nada, e a constraint que garante isso não pode
descartar transação legítima.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.services.ingestion import (
    insert_hashed_transactions,
    prepare_hashed_rows,
    upsert_external_transactions,
)

# Mesmo event loop das fixtures de sessão (ver pyproject.toml).
pytestmark = pytest.mark.asyncio(loop_scope="session")


def pluggy_row(tenant_id, account_id, external_id: str, **overrides):
    row = {
        "tenant_id": tenant_id,
        "account_id": account_id,
        "source": "PLUGGY",
        "external_id": external_id,
        "amount": Decimal("-42.90"),
        "currency_code": "BRL",
        "date": date(2026, 8, 1),
        "description_raw": "PADARIA DO ZE",
        "categorization_status": "PENDING",
    }
    row.update(overrides)
    return row


def csv_row(tenant_id, account_id, **overrides):
    row = {
        "tenant_id": tenant_id,
        "account_id": account_id,
        "source": "CSV",
        "external_id": None,
        "amount": Decimal("-12.00"),
        "currency_code": "BRL",
        "date": date(2026, 8, 1),
        "description_raw": "CAFETERIA CENTRAL",
        "categorization_status": "PENDING",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# (a) Origens com identificador estável — Pluggy e OFX
# ---------------------------------------------------------------------------


async def test_duplicate_external_id_is_rejected(admin_session, tenants, account_a) -> None:
    """Dois INSERTs com o mesmo external_id violam a constraint."""
    tenant_a, _ = tenants
    row = pluggy_row(tenant_a, account_a, "tx-001")

    await upsert_external_transactions(admin_session, [row])
    await admin_session.commit()

    with pytest.raises(IntegrityError):
        await admin_session.execute(
            text(
                "INSERT INTO transactions "
                "(tenant_id, account_id, source, external_id, amount, kind, date, description_raw) "
                "VALUES (:t, :a, 'PLUGGY', 'tx-001', -42.90, 'EXPENSE', "
                "'2026-08-01', 'PADARIA DO ZE')"
            ),
            {"t": str(tenant_a), "a": str(account_a)},
        )
    await admin_session.rollback()


async def test_resync_updates_instead_of_duplicating(admin_session, tenants, account_a) -> None:
    """Transação da Pluggy muda depois de criada (PENDING -> POSTED, valor
    corrigido). O re-sync tem de atualizar a linha existente, não criar outra."""
    tenant_a, _ = tenants

    await upsert_external_transactions(
        admin_session, [pluggy_row(tenant_a, account_a, "tx-002", amount=Decimal("-10.00"))]
    )
    await admin_session.commit()

    await upsert_external_transactions(
        admin_session,
        [
            pluggy_row(
                tenant_a,
                account_a,
                "tx-002",
                amount=Decimal("-15.50"),
                description_raw="PADARIA DO ZE LTDA",
            )
        ],
    )
    await admin_session.commit()

    rows = (
        await admin_session.execute(
            text(
                "SELECT amount, description_raw FROM transactions "
                "WHERE external_id = 'tx-002' AND tenant_id = :t"
            ),
            {"t": str(tenant_a)},
        )
    ).all()

    assert len(rows) == 1
    assert rows[0].amount == Decimal("-15.50")
    assert rows[0].description_raw == "PADARIA DO ZE LTDA"


async def test_resync_preserves_manual_category(admin_session, tenants, account_a) -> None:
    """O teste mais importante desta fase.

    Quando o usuário corrige uma categorização à mão, essa correção é a base de
    regras e de treino que as Fases 3 e 4 querem gerar. Uma re-sincronização
    sobrescrevendo-a com o palpite da Pluggy apagaria esse trabalho em silêncio —
    sem erro, sem log, e só perceptível ao reabrir a tela semanas depois.
    """
    tenant_a, _ = tenants

    category_id = await admin_session.scalar(
        text("SELECT id FROM categories WHERE name = 'Supermercado' LIMIT 1")
    )
    other_category_id = await admin_session.scalar(
        text("SELECT id FROM categories WHERE name = 'Delivery' LIMIT 1")
    )

    await upsert_external_transactions(
        admin_session,
        [
            pluggy_row(
                tenant_a,
                account_a,
                "tx-003",
                category_id=other_category_id,
                category_source="PLUGGY",
                categorization_status="CATEGORIZED",
            )
        ],
    )
    await admin_session.commit()

    # O usuário corrige na tela de revisão.
    await admin_session.execute(
        text(
            "UPDATE transactions SET category_id = :c, category_source = 'MANUAL', "
            "categorization_status = 'CATEGORIZED' WHERE external_id = 'tx-003'"
        ),
        {"c": str(category_id)},
    )
    await admin_session.commit()

    # A Pluggy re-sincroniza a mesma transação insistindo no palpite original.
    await upsert_external_transactions(
        admin_session,
        [
            pluggy_row(
                tenant_a,
                account_a,
                "tx-003",
                amount=Decimal("-99.99"),
                category_id=other_category_id,
                category_source="PLUGGY",
                categorization_status="CATEGORIZED",
            )
        ],
    )
    await admin_session.commit()

    row = (
        await admin_session.execute(
            text(
                "SELECT amount, category_id, category_source FROM transactions "
                "WHERE external_id = 'tx-003'"
            )
        )
    ).one()

    # O valor, que é fato reportado pelo banco, foi atualizado...
    assert row.amount == Decimal("-99.99")
    # ...mas a decisão do usuário sobreviveu.
    assert row.category_id == category_id
    assert row.category_source == "MANUAL"


async def test_resync_preserves_llm_category(admin_session, tenants, account_a) -> None:
    """O que a Fase 3 acrescenta ao teste acima.

    Preservar só `MANUAL` era suficiente enquanto nada mais categorizava. Com o LLM
    gravando `category_source = 'LLM'`, o predicado antigo faria **todo re-sync
    apagar a categorização e reverter o `kind`** — e como a Pluggy coleta sozinha a
    cada ~24h, isso seria o histórico inteiro reprocessado por dia, pagando GPU para
    chegar ao mesmo resultado. Sem erro e sem log.

    O `kind` entra na asserção de propósito: é ele que tira a transferência do total
    de gastos, e revertê-lo é o estrago que menos aparece.
    """
    tenant_a, _ = tenants

    transfer_category_id = await admin_session.scalar(
        text("SELECT id FROM categories WHERE name = 'Pagamento de cartão' LIMIT 1")
    )

    await upsert_external_transactions(
        admin_session, [pluggy_row(tenant_a, account_a, "tx-llm")]
    )
    await admin_session.commit()

    # O job da Fase 3 decide: é pagamento de fatura, logo TRANSFER.
    await admin_session.execute(
        text(
            "UPDATE transactions SET category_id = :c, category_source = 'LLM', "
            "categorization_status = 'CATEGORIZED', category_confidence = 0.910, "
            "kind = 'TRANSFER' WHERE external_id = 'tx-llm'"
        ),
        {"c": str(transfer_category_id)},
    )
    await admin_session.commit()

    # A Pluggy re-sincroniza. O mapeador nunca emite colunas de categoria, então o
    # lote traz `categorization_status = 'PENDING'` e nada mais.
    await upsert_external_transactions(
        admin_session,
        [pluggy_row(tenant_a, account_a, "tx-llm", amount=Decimal("-77.00"))],
    )
    await admin_session.commit()

    row = (
        await admin_session.execute(
            text(
                "SELECT amount, category_id, category_source, categorization_status, "
                "category_confidence, kind FROM transactions WHERE external_id = 'tx-llm'"
            )
        )
    ).one()

    assert row.amount == Decimal("-77.00")
    assert row.category_id == transfer_category_id
    assert row.category_source == "LLM"
    assert row.categorization_status == "CATEGORIZED"
    assert row.category_confidence == Decimal("0.910")
    assert row.kind == "TRANSFER"


async def test_resync_requeues_transaction_without_source(
    admin_session, tenants, account_a
) -> None:
    """A contrapartida: linha sem `category_source` volta para a fila.

    É o caminho de recuperação de uma transação marcada FAILED porque o Ollama
    estava fora do ar. `store.apply_decision` deixa `category_source` NULL nesse
    caso exatamente para que o próximo sync a re-enfileire sozinha.
    """
    tenant_a, _ = tenants

    await upsert_external_transactions(
        admin_session, [pluggy_row(tenant_a, account_a, "tx-failed")]
    )
    await admin_session.execute(
        text(
            "UPDATE transactions SET categorization_status = 'FAILED' "
            "WHERE external_id = 'tx-failed'"
        )
    )
    await admin_session.commit()

    await upsert_external_transactions(
        admin_session, [pluggy_row(tenant_a, account_a, "tx-failed")]
    )
    await admin_session.commit()

    status = await admin_session.scalar(
        text("SELECT categorization_status FROM transactions WHERE external_id = 'tx-failed'")
    )
    assert status == "PENDING"


# ---------------------------------------------------------------------------
# (b) Origens sem identificador — CSV e entrada manual
# ---------------------------------------------------------------------------


async def test_identical_transactions_in_same_batch_both_persist(
    admin_session, tenants, account_a
) -> None:
    """Dois cafés de R$ 12,00 no mesmo dia e no mesmo lugar são duas transações
    reais. Um UNIQUE(conta, data, valor, descrição) descartaria uma delas."""
    tenant_a, _ = tenants

    batch = prepare_hashed_rows([csv_row(tenant_a, account_a), csv_row(tenant_a, account_a)])
    assert [r["dedup_seq"] for r in batch] == [0, 1]
    assert batch[0]["dedup_hash"] == batch[1]["dedup_hash"]

    await insert_hashed_transactions(admin_session, batch)
    await admin_session.commit()

    count = await admin_session.scalar(
        text("SELECT count(*) FROM transactions WHERE tenant_id = :t AND source = 'CSV'"),
        {"t": str(tenant_a)},
    )
    assert count == 2


async def test_reimporting_same_file_is_a_no_op(admin_session, tenants, account_a) -> None:
    """Reimportar o mesmo extrato regenera os mesmos pares (hash, seq), que
    colidem com o que já está gravado."""
    tenant_a, _ = tenants
    rows = [
        csv_row(tenant_a, account_a),
        csv_row(tenant_a, account_a),
        csv_row(tenant_a, account_a, amount=Decimal("-31.40"), description_raw="LIVRARIA"),
    ]

    await insert_hashed_transactions(admin_session, prepare_hashed_rows(rows))
    await admin_session.commit()

    inserted = await insert_hashed_transactions(admin_session, prepare_hashed_rows(rows))
    await admin_session.commit()

    assert inserted == 0
    count = await admin_session.scalar(
        text("SELECT count(*) FROM transactions WHERE tenant_id = :t AND source = 'CSV'"),
        {"t": str(tenant_a)},
    )
    assert count == 3


# O cálculo do hash em si é testado em tests/test_dedup_hash.py (função pura).


# ---------------------------------------------------------------------------
# Invariantes do schema
# ---------------------------------------------------------------------------


async def test_resync_resurrects_a_soft_deleted_transaction(
    admin_session, tenants, account_a
) -> None:
    """`deleted_at` significa "sumiu na origem". Se a instituição voltou a
    reportar a transação, ela existe de novo — mantê-la escondida deixaria o
    extrato do usuário com um saldo que não fecha.

    Enquanto o soft delete tiver esse único significado, ressuscitar é o certo.
    No dia em que existir "excluir" na interface, a exclusão do usuário precisa
    de coluna própria, senão o primeiro re-sync desfaz o que ele mandou apagar.
    """
    tenant_a, _ = tenants
    await upsert_external_transactions(
        admin_session, [pluggy_row(tenant_a, account_a, "tx-del")]
    )
    await admin_session.commit()

    await admin_session.execute(
        text("UPDATE transactions SET deleted_at = now() WHERE external_id = 'tx-del'")
    )
    await admin_session.commit()

    await upsert_external_transactions(
        admin_session, [pluggy_row(tenant_a, account_a, "tx-del")]
    )
    await admin_session.commit()

    rows = (
        await admin_session.execute(
            text("SELECT deleted_at FROM transactions WHERE external_id = 'tx-del'")
        )
    ).all()
    assert len(rows) == 1, "o re-sync não pode criar uma segunda linha"
    assert rows[0].deleted_at is None


async def test_row_without_any_dedup_key_is_rejected(admin_session, tenants, account_a) -> None:
    """Sem external_id nem dedup_hash, a linha não é protegida por nenhum dos dois
    índices — e a duplicação só apareceria no extrato do usuário."""
    tenant_a, _ = tenants
    with pytest.raises(IntegrityError, match="ck_transactions_dedup_key_present"):
        await admin_session.execute(
            text(
                "INSERT INTO transactions "
                "(tenant_id, account_id, source, amount, kind, date, description_raw) "
                "VALUES (:t, :a, 'MANUAL', -1.00, 'EXPENSE', '2026-08-01', 'SEM CHAVE')"
            ),
            {"t": str(tenant_a), "a": str(account_a)},
        )
    await admin_session.rollback()


async def test_amount_keeps_exact_decimal_scale(admin_session, tenants, account_a) -> None:
    """NUMERIC e não float: dez lançamentos de R$ 0,10 somam exatamente R$ 1,00.

    Era o defeito do protótipo SQLite, que usava REAL — em ponto flutuante
    binário essa soma dá 0.9999999999999999.
    """
    tenant_a, _ = tenants
    rows = prepare_hashed_rows(
        [
            csv_row(
                tenant_a,
                account_a,
                amount=Decimal("0.10"),
                description_raw=f"CENTAVO {i}",
            )
            for i in range(10)
        ]
    )
    await insert_hashed_transactions(admin_session, rows)
    await admin_session.commit()

    total = await admin_session.scalar(
        text("SELECT sum(amount) FROM transactions WHERE tenant_id = :t AND source = 'CSV'"),
        {"t": str(tenant_a)},
    )
    assert total == Decimal("1.00")


async def test_updated_at_advances_on_raw_sql_update(admin_session, tenants, account_a) -> None:
    """A trigger mantém `updated_at` correto mesmo fora do ORM.

    Importa porque o sync grava via INSERT ... ON CONFLICT, que não passa pelo
    SQLAlchemy — com `onupdate` do ORM, o timestamp ficaria congelado justamente
    nas linhas atualizadas por re-sincronização.
    """
    tenant_a, _ = tenants
    await upsert_external_transactions(admin_session, [pluggy_row(tenant_a, account_a, "tx-004")])
    await admin_session.commit()

    before = await admin_session.scalar(
        text("SELECT updated_at FROM transactions WHERE external_id = 'tx-004'")
    )

    await admin_session.execute(
        text("UPDATE transactions SET description_clean = 'Padaria' WHERE external_id = 'tx-004'")
    )
    await admin_session.commit()

    after = await admin_session.scalar(
        text("SELECT updated_at FROM transactions WHERE external_id = 'tx-004'")
    )
    assert after > before
