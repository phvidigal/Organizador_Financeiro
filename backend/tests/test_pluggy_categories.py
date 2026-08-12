"""De/para entre a taxonomia da Pluggy e a nossa.

Preencher `categories.pluggy_category_id` é o que permite à Fase 3 comparar o
palpite da Pluggy com a escolha do LLM — divergência entre duas fontes
independentes é sinal de confiança melhor que a autoavaliação de uma só.

`PLUGGY_TO_LOCAL` está vazio no código: as entradas saem da resposta real de
`GET /categories`, e chutá-las produziria mapeamento errado que só apareceria
quando alguém conferisse uma categorização na tela. Aqui ele é injetado.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import DEFAULT_TENANT_ID
from app.services.pluggy import category_map
from app.services.pluggy.category_map import sync_category_map
from app.services.pluggy.client import _loads
from tests.conftest import TENANT_A
from tests.pluggy_fixtures import CATEGORIES_JSON

pytestmark = pytest.mark.asyncio(loop_scope="session")

PLUGGY_CATEGORIES = _loads(CATEGORIES_JSON.encode())["results"]


@pytest.fixture
async def local_categories(admin_session: AsyncSession, tenants) -> dict[str, uuid.UUID]:
    """Duas categorias raiz do tenant A, sem mapeamento nenhum."""
    tenant_a, _ = tenants
    ids = {"Alimentação": uuid.uuid4(), "Receitas": uuid.uuid4()}
    for name, category_id in ids.items():
        kind = "INCOME" if name == "Receitas" else "EXPENSE"
        await admin_session.execute(
            text(
                "INSERT INTO categories (id, tenant_id, name, kind) "
                "VALUES (:id, :tenant_id, :name, :kind)"
            ),
            {"id": str(category_id), "tenant_id": str(tenant_a), "name": name, "kind": kind},
        )
    await admin_session.commit()
    return ids


async def mapped_ids(app_tenant_session) -> dict[str, str | None]:
    async with app_tenant_session(TENANT_A) as session:
        rows = await session.execute(text("SELECT name, pluggy_category_id FROM categories"))
        return {row.name: row.pluggy_category_id for row in rows}


async def test_mapping_fills_the_pluggy_id(
    local_categories, app_tenant_session, monkeypatch
) -> None:
    monkeypatch.setattr(
        category_map,
        "PLUGGY_TO_LOCAL",
        {"Food and drinks": "Alimentação", "Income": "Receitas"},
    )

    async with app_tenant_session(TENANT_A) as session:
        report = await sync_category_map(session, pluggy_categories=PLUGGY_CATEGORIES)

    assert report.mapped == 2
    assert await mapped_ids(app_tenant_session) == {
        "Alimentação": "05000000",
        "Receitas": "01000000",
    }


async def test_mapping_is_idempotent(local_categories, app_tenant_session, monkeypatch) -> None:
    """Roda a cada sync. Reexecutar não pode mexer em nada."""
    monkeypatch.setattr(category_map, "PLUGGY_TO_LOCAL", {"Food and drinks": "Alimentação"})

    async with app_tenant_session(TENANT_A) as session:
        first = await sync_category_map(session, pluggy_categories=PLUGGY_CATEGORIES)
    async with app_tenant_session(TENANT_A) as session:
        second = await sync_category_map(session, pluggy_categories=PLUGGY_CATEGORIES)

    assert (first.mapped, first.already_mapped) == (1, 0)
    assert (second.mapped, second.already_mapped) == (0, 1)
    assert not second.conflicts


async def test_empty_dictionary_reports_what_is_missing(
    local_categories, app_tenant_session, monkeypatch
) -> None:
    """Com o de/para vazio, o relatório é o dump que permite escrevê-lo — foi assim
    que `PLUGGY_TO_LOCAL` saiu das 130 categorias reais."""
    monkeypatch.setattr(category_map, "PLUGGY_TO_LOCAL", {})

    async with app_tenant_session(TENANT_A) as session:
        report = await sync_category_map(session, pluggy_categories=PLUGGY_CATEGORIES)

    assert report.mapped == 0
    assert report.unmatched_pluggy == ["Food and drinks", "Income", "Restaurants"]
    assert set(report.unmapped_local) == {"Alimentação", "Receitas"}


async def test_two_pluggy_categories_cannot_claim_the_same_local_one(
    local_categories, app_tenant_session, monkeypatch
) -> None:
    """`pluggy_category_id` é uma coluna só. Sem esta guarda, a última entrada do
    dicionário venceria — e quem vence dependeria da ordem de iteração."""
    monkeypatch.setattr(
        category_map,
        "PLUGGY_TO_LOCAL",
        {"Food and drinks": "Alimentação", "Restaurants": "Alimentação"},
    )

    async with app_tenant_session(TENANT_A) as session:
        report = await sync_category_map(session, pluggy_categories=PLUGGY_CATEGORIES)

    assert report.mapped == 0
    assert "reivindicada por mais de uma" in report.conflicts[0]
    assert (await mapped_ids(app_tenant_session))["Alimentação"] is None


async def test_the_real_dictionary_agrees_with_the_seeded_taxonomy(
    admin_session: AsyncSession,
) -> None:
    """Guarda contra o de/para envelhecer em silêncio.

    Renomear uma categoria semeada na migration deixaria a entrada correspondente
    apontando para o vazio, e o mapeamento simplesmente pararia de acontecer sem
    nenhum erro. Aqui isso vira um teste vermelho.
    """
    rows = await admin_session.execute(
        text("SELECT name FROM categories WHERE tenant_id = :t"),
        {"t": str(DEFAULT_TENANT_ID)},
    )
    seeded = {row.name for row in rows}
    assert seeded, "o seed do tenant padrão sumiu"

    alvos = list(category_map.PLUGGY_TO_LOCAL.values())

    inexistentes = sorted(set(alvos) - seeded)
    assert not inexistentes, f"o de/para aponta para categorias que não existem: {inexistentes}"

    # 1:1 — ver a nota sobre o schema no topo de `category_map.py`.
    repetidos = sorted({nome for nome in alvos if alvos.count(nome) > 1})
    assert not repetidos, f"categorias reivindicadas mais de uma vez: {repetidos}"


async def test_existing_mapping_is_never_silently_overwritten(
    local_categories, admin_session, app_tenant_session, monkeypatch
) -> None:
    """Divergência significa que a Pluggy renomeou ou renumerou uma categoria.
    Resolver isso em silêncio faria a Fase 3 medir acerto contra um de/para errado."""
    await admin_session.execute(
        text("UPDATE categories SET pluggy_category_id = '99999999' WHERE name = 'Receitas'")
    )
    await admin_session.commit()
    monkeypatch.setattr(category_map, "PLUGGY_TO_LOCAL", {"Income": "Receitas"})

    async with app_tenant_session(TENANT_A) as session:
        report = await sync_category_map(session, pluggy_categories=PLUGGY_CATEGORIES)

    assert report.mapped == 0
    assert len(report.conflicts) == 1
    assert "99999999" in report.conflicts[0]
    assert (await mapped_ids(app_tenant_session))["Receitas"] == "99999999"


async def test_ambiguous_local_name_is_a_conflict_not_a_guess(
    local_categories, admin_session, tenants, app_tenant_session, monkeypatch
) -> None:
    """O índice único é `(tenant_id, parent_id, name)`, então o mesmo nome pode
    existir sob pais diferentes. Escolher um dos dois seria um chute."""
    tenant_a, _ = tenants
    await admin_session.execute(
        text(
            "INSERT INTO categories (id, tenant_id, parent_id, name, kind) "
            "VALUES (:id, :tenant_id, :parent, 'Alimentação', 'EXPENSE')"
        ),
        {
            "id": str(uuid.uuid4()),
            "tenant_id": str(tenant_a),
            "parent": str(local_categories["Receitas"]),
        },
    )
    await admin_session.commit()
    monkeypatch.setattr(category_map, "PLUGGY_TO_LOCAL", {"Food and drinks": "Alimentação"})

    async with app_tenant_session(TENANT_A) as session:
        report = await sync_category_map(session, pluggy_categories=PLUGGY_CATEGORIES)

    assert report.mapped == 0
    assert "é ambígua" in report.conflicts[0]


async def test_dictionary_entry_absent_from_the_api_is_flagged(
    local_categories, app_tenant_session, monkeypatch
) -> None:
    """Entrada obsoleta no dicionário não pode passar despercebida."""
    monkeypatch.setattr(category_map, "PLUGGY_TO_LOCAL", {"Bitcoin mining": "Alimentação"})

    async with app_tenant_session(TENANT_A) as session:
        report = await sync_category_map(session, pluggy_categories=PLUGGY_CATEGORIES)

    assert report.mapped == 0
    assert "não veio de GET /categories" in report.conflicts[0]
