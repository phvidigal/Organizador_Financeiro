"""As migrations sobem e descem limpas.

Testar o downgrade não é preciosismo: é a diferença entre poder reverter um
deploy ruim e ter de restaurar backup. E como esta migration cria objetos que o
`drop_table` não remove sozinho (policies, triggers, uma função), o caminho de
volta é justamente onde o esquecimento aparece.
"""

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.services.pluggy.client import _loads
from app.services.pluggy.mappers import jsonable, minimize_raw_payload
from tests.conftest import (
    create_database,
    downgrade_migrations,
    drop_database,
    run_migrations,
    url_for_database,
)
from tests.pluggy_fixtures import TRANSACTION_WITH_PERSONAL_DATA_JSON

# Mesmo event loop das fixtures de sessão (ver pyproject.toml).
pytestmark = pytest.mark.asyncio(loop_scope="session")

EXPECTED_TABLES = {
    "tenants",
    "users",
    "bank_connections",
    "accounts",
    "categories",
    "transactions",
    "category_rules",
}


async def test_upgrade_then_downgrade_leaves_no_residue() -> None:
    db_name = f"migration_check_{uuid.uuid4().hex[:8]}"
    await create_database(db_name)
    url = url_for_database(db_name)

    try:
        run_migrations(url)

        engine = create_async_engine(url)
        async with engine.connect() as conn:
            tables = set(
                (
                    await conn.execute(
                        text(
                            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert tables >= EXPECTED_TABLES

            # A função da trigger existe e está ligada a todas as tabelas.
            trigger_count = await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger "
                    "WHERE tgname LIKE 'trg_%_updated_at' AND NOT tgisinternal"
                )
            )
            assert trigger_count == len(EXPECTED_TABLES)
        await engine.dispose()

        downgrade_migrations(url)

        engine = create_async_engine(url)
        async with engine.connect() as conn:
            remaining = set(
                (
                    await conn.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                    )
                )
                .scalars()
                .all()
            )
            assert not (EXPECTED_TABLES & remaining)

            # A função da trigger é objeto solto: nenhum DROP TABLE a remove, e
            # esquecê-la no downgrade faz o próximo upgrade encontrar um objeto
            # pré-existente.
            leftover_function = await conn.scalar(
                text("SELECT count(*) FROM pg_proc WHERE proname = 'set_updated_at'")
            )
            assert leftover_function == 0
        await engine.dispose()
    finally:
        await drop_database(db_name)


async def test_seed_creates_default_tenant_and_categories(admin_session) -> None:
    """O tenant padrão e a taxonomia em pt-BR chegam pela migration."""
    tenant_name = await admin_session.scalar(
        text("SELECT name FROM tenants WHERE id = '00000000-0000-0000-0000-000000000001'")
    )
    assert tenant_name == "Pessoal"

    roots = await admin_session.scalar(
        text("SELECT count(*) FROM categories WHERE parent_id IS NULL AND is_system")
    )
    children = await admin_session.scalar(
        text("SELECT count(*) FROM categories WHERE parent_id IS NOT NULL AND is_system")
    )
    # 11 da migration inicial + "Investimentos" (0003).
    assert roots == 12
    assert children > 0

    # O de/para com a taxonomia da Pluggy é preenchido na Fase 2, a partir de
    # GET /categories. Chutar os ids agora produziria mapeamentos errados que
    # ninguém notaria até conferir uma categorização no dashboard.
    unmapped = await admin_session.scalar(
        text("SELECT count(*) FROM categories WHERE pluggy_category_id IS NOT NULL")
    )
    assert unmapped == 0


async def test_investment_tree_is_transfer_not_expense(admin_session) -> None:
    """Aplicar não é gastar, resgatar não é receber.

    Se a árvore nascesse EXPENSE, um mês com R$ 5.000 aplicados apareceria como
    R$ 5.000 de gasto — e o resgate dos mesmos R$ 5.000, depois, como receita. O
    mesmo dinheiro contado duas vezes é exatamente o que o campo `kind` existe
    para evitar.
    """
    rows = (
        await admin_session.execute(
            text(
                "SELECT c.name, c.kind FROM categories c "
                "LEFT JOIN categories p ON p.id = c.parent_id "
                "WHERE c.name = 'Investimentos' OR p.name = 'Investimentos'"
            )
        )
    ).all()

    assert {row.name for row in rows} == {
        "Investimentos",
        "Renda fixa",
        "Renda variável",
        "Fundos de investimento",
        "Criptoativos",
        "Previdência privada",
    }
    assert {row.kind for row in rows} == {"TRANSFER"}

    # O rendimento é dinheiro novo e continua sendo receita, numa árvore separada.
    yield_kind = await admin_session.scalar(
        text("SELECT kind FROM categories WHERE name = 'Rendimentos e investimentos'")
    )
    assert yield_kind == "INCOME"


async def test_0006_strips_personal_data_already_stored() -> None:
    """A linha gravada antes da `0006` — com `paymentData` inteiro, como o sync
    gravava — sai dela igual ao que `minimize_raw_payload` produz hoje.

    É a guarda da cópia congelada: a migration não importa a função, de propósito,
    e este teste é o que garante que as duas listas eram a mesma no dia em que a
    limpeza rodou.
    """
    db_name = f"migration_0006_{uuid.uuid4().hex[:8]}"
    await create_database(db_name)
    url = url_for_database(db_name)
    tx = _loads(TRANSACTION_WITH_PERSONAL_DATA_JSON.encode())

    try:
        run_migrations(url, "0005_categorization_reviews")

        engine = create_async_engine(url)
        async with engine.begin() as conn:
            account_id = uuid.uuid4()
            await conn.execute(
                text(
                    "INSERT INTO accounts (id, tenant_id, type, name) VALUES "
                    "(:id, '00000000-0000-0000-0000-000000000001', 'BANK', 'Conta')"
                ),
                {"id": str(account_id)},
            )
            # SQL direto, e não `ingestion.py`: o objetivo é reproduzir o estado que
            # o código *antigo* deixou, e o código atual já não grava assim.
            await conn.execute(
                text(
                    "INSERT INTO transactions (tenant_id, account_id, source, external_id, "
                    "amount, kind, date, description_raw, raw_payload) VALUES "
                    "('00000000-0000-0000-0000-000000000001', :account, 'PLUGGY', 'pii-1', "
                    "850.00, 'INCOME', DATE '2026-08-05', 'PIX', CAST(:payload AS jsonb))"
                ),
                {"account": str(account_id), "payload": json.dumps(jsonable(tx))},
            )
        await engine.dispose()

        run_migrations(url)

        engine = create_async_engine(url)
        async with engine.connect() as conn:
            stored = await conn.scalar(
                text("SELECT raw_payload FROM transactions WHERE external_id = 'pii-1'")
            )
        await engine.dispose()

        assert stored == minimize_raw_payload(tx)
        assert "Fulano de Tal" not in json.dumps(stored)
    finally:
        await drop_database(db_name)


async def test_0008_pix_enviado_becomes_expense() -> None:
    """`Pix enviado` muda de árvore e de natureza, e as transações acompanham.

    A decisão do LLM volta para a fila — foi tomada sob o significado antigo — e a
    correção do titular fica, só herdando o `kind` novo. O caminho de volta desfaz
    os dois lados.
    """
    db_name = f"migration_0008_{uuid.uuid4().hex[:8]}"
    await create_database(db_name)
    url = url_for_database(db_name)
    tenant = "00000000-0000-0000-0000-000000000001"

    async def state(conn) -> dict[str, tuple]:
        rows = await conn.execute(
            text(
                "SELECT external_id, category_id IS NOT NULL, category_source, "
                "categorization_status, kind FROM transactions ORDER BY external_id"
            )
        )
        return {row[0]: tuple(row[1:]) for row in rows}

    try:
        run_migrations(url, "0007_counterparty_and_rule")

        engine = create_async_engine(url)
        async with engine.begin() as conn:
            pix_enviado = await conn.scalar(
                text(
                    "SELECT c.id FROM categories c JOIN categories p ON p.id = c.parent_id "
                    "WHERE c.name = 'Pix enviado' AND p.name = 'Transferências'"
                )
            )
            account_id = uuid.uuid4()
            await conn.execute(
                text(
                    "INSERT INTO accounts (id, tenant_id, type, name) "
                    "VALUES (:id, :tenant, 'BANK', 'Conta')"
                ),
                {"id": str(account_id), "tenant": tenant},
            )
            for external_id, source in (("llm", "LLM"), ("manual", "MANUAL")):
                await conn.execute(
                    text(
                        "INSERT INTO transactions (tenant_id, account_id, source, "
                        "external_id, amount, kind, date, description_raw, category_id, "
                        "category_source, categorization_status) VALUES (:tenant, "
                        ":account, 'PLUGGY', :ext, -80.00, 'TRANSFER', DATE '2026-08-05', "
                        "'PIX ENVIADO FULANO', :category, :source, 'CATEGORIZED')"
                    ),
                    {
                        "tenant": tenant,
                        "account": str(account_id),
                        "ext": external_id,
                        "category": str(pix_enviado),
                        "source": source,
                    },
                )
        await engine.dispose()

        run_migrations(url)

        engine = create_async_engine(url)
        async with engine.connect() as conn:
            category = (
                await conn.execute(
                    text(
                        "SELECT p.name, c.kind FROM categories c "
                        "JOIN categories p ON p.id = c.parent_id WHERE c.id = :id"
                    ),
                    {"id": str(pix_enviado)},
                )
            ).one()
            after = await state(conn)
        await engine.dispose()

        assert tuple(category) == ("Outros", "EXPENSE")
        assert after["llm"] == (False, None, "PENDING", "EXPENSE")
        assert after["manual"] == (True, "MANUAL", "CATEGORIZED", "EXPENSE")

        downgrade_migrations(url, "0007_counterparty_and_rule")

        engine = create_async_engine(url)
        async with engine.connect() as conn:
            kind = await conn.scalar(
                text("SELECT kind FROM categories WHERE id = :id"), {"id": str(pix_enviado)}
            )
            back = await state(conn)
        await engine.dispose()

        assert kind == "TRANSFER"
        assert back["manual"] == (True, "MANUAL", "CATEGORIZED", "TRANSFER")
    finally:
        await drop_database(db_name)
