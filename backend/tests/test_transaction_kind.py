"""Natureza da transação (`kind`) — o que faz o dashboard somar certo.

Sem este campo, um Pix de R$ 2.000 entre contas do próprio titular apareceria
como R$ 2.000 de despesa numa conta e R$ 2.000 de receita na outra. E contar cada
compra do cartão como despesa na data da compra só é seguro se o pagamento da
fatura ficar de fora da soma.
"""

# As funções puras (derive_kind) são testadas em tests/test_ingestion_pure.py.

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

pytestmark = pytest.mark.asyncio(loop_scope="session")


def row(tenant_id, account_id, **overrides):
    base = {
        "tenant_id": tenant_id,
        "account_id": account_id,
        "source": "CSV",
        "external_id": None,
        "amount": Decimal("-50.00"),
        "currency_code": "BRL",
        "date": date(2026, 8, 1),
        "description_raw": "COMPRA",
        "categorization_status": "PENDING",
    }
    base.update(overrides)
    return base


async def test_invalid_kind_is_rejected(admin_session, tenants, account_a) -> None:
    tenant_a, _ = tenants
    with pytest.raises(IntegrityError, match="ck_transactions_transaction_kind"):
        await admin_session.execute(
            text(
                "INSERT INTO transactions "
                "(tenant_id, account_id, source, external_id, amount, kind, date, description_raw) "
                "VALUES (:t, :a, 'PLUGGY', 'k-bad', -1.00, 'GASTO', '2026-08-01', 'X')"
            ),
            {"t": str(tenant_a), "a": str(account_a)},
        )
    await admin_session.rollback()


async def test_seeded_categories_declare_their_kind(admin_session) -> None:
    """A taxonomia semeada é a fonte do `kind`: quando uma transação é
    categorizada, ela herda daqui. Sem isso, cada camada da pipeline teria de
    lembrar de ajustar o `kind` junto, e a que esquecesse deixaria um Pix
    contando como gasto."""
    kinds = dict(
        (
            await admin_session.execute(
                text("SELECT name, kind FROM categories WHERE parent_id IS NULL AND is_system")
            )
        ).all()
    )
    assert kinds["Receitas"] == "INCOME"
    assert kinds["Transferências"] == "TRANSFER"
    assert kinds["Alimentação"] == "EXPENSE"

    # Subcategoria herda a raiz, sem exceção: é isso que permite ao job gravar o
    # `kind` sem conhecer nome de categoria.
    filhos_transferencia = (
        await admin_session.execute(
            text(
                "SELECT DISTINCT c.kind FROM categories c "
                "JOIN categories p ON p.id = c.parent_id WHERE p.name = 'Transferências'"
            )
        )
    ).scalars().all()
    assert filhos_transferencia == ["TRANSFER"]


async def test_pix_recebido_is_income_not_transfer(admin_session) -> None:
    """A correção que a primeira rodada completa exigiu (migration 0004).

    Sob `Transferências`, "Pix recebido" tratava **todo** Pix recebido como dinheiro
    andando entre contas do próprio titular — premissa falsa para quem recebe
    pagamento por Pix. Num extrato real isso pôs mais de 99% do dinheiro que
    entrava fora dos totais, deixando `Receitas` só com rendimento de investimento.

    O caso legítimo de conta própria continua tendo casa, e é ela que precisa
    sobreviver a esta mudança.
    """
    rows = dict(
        (
            await admin_session.execute(
                text(
                    "SELECT c.name, p.name FROM categories c "
                    "JOIN categories p ON p.id = c.parent_id "
                    "WHERE c.name IN ('Pix recebido', 'Transferência entre contas próprias')"
                )
            )
        ).all()
    )
    assert rows["Pix recebido"] == "Receitas"
    assert rows["Transferência entre contas próprias"] == "Transferências"

    kind = await admin_session.scalar(
        text("SELECT kind FROM categories WHERE name = 'Pix recebido'")
    )
    assert kind == "INCOME"


async def test_transfer_is_excluded_from_expense_total(
    admin_session, tenants, account_a
) -> None:
    """O caso que motivou o campo: um Pix de R$ 2.000 entre contas próprias não
    pode entrar no total de gastos do mês."""
    tenant_a, _ = tenants

    await insert_hashed_transactions(
        admin_session,
        prepare_hashed_rows(
            [
                row(tenant_a, account_a, amount=Decimal("-50.00"), description_raw="MERCADO"),
                row(tenant_a, account_a, amount=Decimal("-30.00"), description_raw="FARMACIA"),
                # Transferência já classificada (o que a Fase 2 fará ao categorizar).
                row(
                    tenant_a,
                    account_a,
                    amount=Decimal("-2000.00"),
                    description_raw="PIX CONTA PROPRIA",
                    kind="TRANSFER",
                ),
                row(
                    tenant_a,
                    account_a,
                    amount=Decimal("5000.00"),
                    description_raw="SALARIO",
                    kind="INCOME",
                ),
            ]
        ),
    )
    await admin_session.commit()

    totals = dict(
        (
            await admin_session.execute(
                text(
                    "SELECT kind, sum(amount) FROM transactions "
                    "WHERE tenant_id = :t GROUP BY kind"
                ),
                {"t": str(tenant_a)},
            )
        ).all()
    )

    assert totals["EXPENSE"] == Decimal("-80.00")
    assert totals["INCOME"] == Decimal("5000.00")
    assert totals["TRANSFER"] == Decimal("-2000.00")

    # Sem o campo, este total seria -2080.00: R$ 2.000 de gasto que nunca existiu.
    assert totals["EXPENSE"] != Decimal("-2080.00")


async def test_credit_card_purchase_and_bill_payment_do_not_double_count(
    admin_session, tenants, account_a
) -> None:
    """Você escolheu contar a compra na data da compra. Isso só fecha se o
    pagamento da fatura ficar fora do total — senão os mesmos R$ 300 entram duas
    vezes, uma como compra e outra como pagamento."""
    tenant_a, _ = tenants

    await insert_hashed_transactions(
        admin_session,
        prepare_hashed_rows(
            [
                row(tenant_a, account_a, amount=Decimal("-300.00"), description_raw="LOJA X"),
                row(
                    tenant_a,
                    account_a,
                    amount=Decimal("-300.00"),
                    description_raw="PAGAMENTO FATURA CARTAO",
                    kind="TRANSFER",
                ),
            ]
        ),
    )
    await admin_session.commit()

    gasto = await admin_session.scalar(
        text(
            "SELECT sum(amount) FROM transactions "
            "WHERE tenant_id = :t AND kind = 'EXPENSE'"
        ),
        {"t": str(tenant_a)},
    )
    assert gasto == Decimal("-300.00")


async def test_resync_preserves_manual_kind(admin_session, tenants, account_a) -> None:
    """Marcar um Pix como transferência é decisão do usuário tanto quanto escolher
    a categoria. Um re-sync revertendo para EXPENSE traria o valor de volta ao
    total de gastos sem nenhum sinal de que algo mudou."""
    tenant_a, _ = tenants

    pluggy = {
        "tenant_id": tenant_a,
        "account_id": account_a,
        "source": "PLUGGY",
        "external_id": "kind-001",
        "amount": Decimal("-2000.00"),
        "currency_code": "BRL",
        "date": date(2026, 8, 1),
        "description_raw": "PIX ENVIADO",
        "categorization_status": "PENDING",
    }

    await upsert_external_transactions(admin_session, [pluggy])
    await admin_session.commit()
    assert (
        await admin_session.scalar(
            text("SELECT kind FROM transactions WHERE external_id = 'kind-001'")
        )
        == "EXPENSE"
    )

    # O usuário marca como transferência entre contas próprias.
    await admin_session.execute(
        text(
            "UPDATE transactions SET kind = 'TRANSFER', category_source = 'MANUAL', "
            "categorization_status = 'CATEGORIZED' WHERE external_id = 'kind-001'"
        )
    )
    await admin_session.commit()

    # A Pluggy re-sincroniza a mesma transação.
    await upsert_external_transactions(admin_session, [pluggy])
    await admin_session.commit()

    assert (
        await admin_session.scalar(
            text("SELECT kind FROM transactions WHERE external_id = 'kind-001'")
        )
        == "TRANSFER"
    )
