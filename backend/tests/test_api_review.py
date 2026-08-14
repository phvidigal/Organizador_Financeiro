"""Correção manual de categoria — o endpoint que produz a régua da Fase 4.

`PATCH /transactions/{id}` é o único caminho do sistema que grava
`category_source = 'MANUAL'`, e com ele a base de regras da pipeline híbrida e a
única medida do acerto do LLM. Duas coisas precisam ser verdade ao mesmo tempo, e
elas puxam em direções opostas:

* a correção **sobrescreve** a decisão do LLM — é o objetivo;
* a decisão do LLM **não pode se perder** — é a medição.

É a linha de `categorization_reviews` que concilia as duas, e é isso que o grosso
deste módulo verifica. O resto cobre o que já era invariante antes: `kind` andando
junto da categoria, e a correção sobrevivendo a uma re-sincronização.

Mesmo desenho de `test_api_categorization.py`: `httpx.ASGITransport`, sem lifespan
e sem servidor, com a sessão entrando por `dependency_overrides`.
"""

import uuid
from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import get_tenant_session, resolve_tenant_id
from app.main import create_app
from app.models.enums import CategorizationStatus, CategorySource, TransactionKind
from app.services.ingestion import upsert_external_transactions
from app.services.pluggy.sync import sync_connection
from tests.conftest import TENANT_A
from tests.pluggy_fixtures import ITEM_ID, default_routes, make_client

# Mesmo event loop das fixtures de sessão (ver pyproject.toml).
pytestmark = pytest.mark.asyncio(loop_scope="session")

EXTERNAL_ID = "review-0001"


@pytest.fixture
def api(app_tenant_session):
    def build():
        app = create_app()

        async def override_session():
            async with app_tenant_session(TENANT_A) as session:
                yield session

        app.dependency_overrides[get_tenant_session] = override_session
        app.dependency_overrides[resolve_tenant_id] = lambda: TENANT_A
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    return build


@pytest.fixture
async def categories(admin_session: AsyncSession, tenants) -> dict[str, uuid.UUID]:
    """Uma árvore mínima com os dois `kind` que importam, mais uma desativada.

    Dois níveis de propósito: `load_catalog` monta rótulos qualificados, e uma
    taxonomia plana não exercitaria o caminho que a tela de revisão usa.
    """
    tenant_a, _ = tenants
    ids = {
        "transferencias": uuid.uuid4(),
        "pix_enviado": uuid.uuid4(),
        "alimentacao": uuid.uuid4(),
        "desativada": uuid.uuid4(),
    }
    await admin_session.execute(
        text(
            "INSERT INTO categories (id, tenant_id, parent_id, name, kind, is_active) VALUES "
            "(:t, :tenant, NULL, 'Transferências', 'TRANSFER', true), "
            "(:p, :tenant, :t,   'Pix enviado',    'TRANSFER', true), "
            "(:a, :tenant, NULL, 'Alimentação',    'EXPENSE',  true), "
            "(:d, :tenant, NULL, 'Aposentada',     'EXPENSE',  false)"
        ),
        {
            "tenant": str(tenant_a),
            "t": str(ids["transferencias"]),
            "p": str(ids["pix_enviado"]),
            "a": str(ids["alimentacao"]),
            "d": str(ids["desativada"]),
        },
    )
    await admin_session.commit()
    return ids


@pytest.fixture
async def transaction_id(app_tenant_session, tenants, account_a) -> uuid.UUID:
    """Uma transação que o LLM decidiu com confiança baixa — o caso típico da fila."""
    tenant_a, _ = tenants
    async with app_tenant_session(tenant_a) as session:
        await upsert_external_transactions(
            session,
            [
                {
                    "tenant_id": tenant_a,
                    "account_id": account_a,
                    "source": "PLUGGY",
                    "external_id": EXTERNAL_ID,
                    "amount": Decimal("-250.00"),
                    "currency_code": "BRL",
                    "date": date(2026, 8, 1),
                    "description_raw": "PIX ENVIADO",
                    "categorization_status": "PENDING",
                }
            ],
        )

    async with app_tenant_session(tenant_a) as session:
        return await session.scalar(
            text("SELECT id FROM transactions WHERE external_id = :e"), {"e": EXTERNAL_ID}
        )


async def apply_llm_decision(session_scope, transaction_id, category_id, confidence) -> None:
    """Põe a transação no estado em que a tela de revisão a encontra."""
    async with session_scope(TENANT_A) as session:
        await session.execute(
            text(
                "UPDATE transactions SET category_id = :c, category_source = 'LLM', "
                "categorization_status = 'NEEDS_REVIEW', category_confidence = :conf, "
                "kind = 'TRANSFER' WHERE id = :id"
            ),
            {"c": str(category_id), "conf": confidence, "id": str(transaction_id)},
        )


async def fetch_transaction(session_scope, transaction_id):
    async with session_scope(TENANT_A) as session:
        return (
            await session.execute(
                text(
                    "SELECT category_id, category_source, categorization_status, "
                    "       category_confidence, categorized_at, kind "
                    "FROM transactions WHERE id = :id"
                ),
                {"id": str(transaction_id)},
            )
        ).one()


async def fetch_reviews(session_scope):
    async with session_scope(TENANT_A) as session:
        return list(
            await session.execute(
                text(
                    "SELECT transaction_id, previous_category_id, previous_kind, "
                    "       previous_source, previous_status, previous_confidence, "
                    "       new_category_id, new_kind "
                    "FROM categorization_reviews ORDER BY created_at"
                )
            )
        )


# ---------------------------------------------------------------------------
# A escrita
# ---------------------------------------------------------------------------


async def test_patch_grava_as_seis_colunas_como_manual(
    api, app_tenant_session, categories, transaction_id
) -> None:
    """Categoria, origem, status, confiança, timestamp e `kind`, numa tacada.

    `kind` é a que se esquece: sem herdá-la de `categories.kind`, a linha continua
    contando como gasto no dashboard mesmo apontando para "Transferências".
    """
    async with api() as client:
        response = await client.patch(
            f"/transactions/{transaction_id}",
            json={"category_id": str(categories["pix_enviado"])},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["category_source"] == CategorySource.MANUAL
    assert body["categorization_status"] == CategorizationStatus.CATEGORIZED
    assert body["kind"] == TransactionKind.TRANSFER

    row = await fetch_transaction(app_tenant_session, transaction_id)
    assert row.category_id == categories["pix_enviado"]
    assert row.category_source == CategorySource.MANUAL
    assert row.categorization_status == CategorizationStatus.CATEGORIZED
    assert row.kind == TransactionKind.TRANSFER
    assert row.categorized_at is not None
    # Confiança é a autoavaliação de um modelo; escolha humana não tem análogo.
    assert row.category_confidence is None


async def test_kind_pode_ser_sobreposto(
    api, app_tenant_session, categories, transaction_id
) -> None:
    """O caso que o `SYSTEM_PROMPT` não resolve: `Pix enviado` continua TRANSFER,
    mas pagar alguém por um serviço é despesa. Só o titular sabe qual dos dois é."""
    async with api() as client:
        response = await client.patch(
            f"/transactions/{transaction_id}",
            json={"category_id": str(categories["pix_enviado"]), "kind": "EXPENSE"},
        )

    assert response.status_code == 200
    row = await fetch_transaction(app_tenant_session, transaction_id)
    # A categoria continua sendo a TRANSFER escolhida — os dois eixos são separados.
    assert row.category_id == categories["pix_enviado"]
    assert row.kind == TransactionKind.EXPENSE


async def test_a_revisao_guarda_a_decisao_anterior_do_llm(
    api, app_tenant_session, categories, transaction_id
) -> None:
    """A razão de a tabela existir.

    Sem esta linha, depois do UPDATE não dá para saber se o titular **confirmou** a
    escolha do LLM ou a **corrigiu** — e é exatamente essa diferença que responde se
    `0.450` erra mais que `0.950`.
    """
    await apply_llm_decision(
        app_tenant_session, transaction_id, categories["transferencias"], Decimal("0.450")
    )

    async with api() as client:
        await client.patch(
            f"/transactions/{transaction_id}",
            json={"category_id": str(categories["alimentacao"])},
        )

    reviews = await fetch_reviews(app_tenant_session)
    assert len(reviews) == 1
    review = reviews[0]
    assert review.transaction_id == transaction_id
    assert review.previous_category_id == categories["transferencias"]
    assert review.previous_source == CategorySource.LLM
    assert review.previous_status == CategorizationStatus.NEEDS_REVIEW
    assert review.previous_kind == TransactionKind.TRANSFER
    # O número **cru** do modelo, que a transação acabou de perder.
    assert review.previous_confidence == Decimal("0.450")
    assert review.new_category_id == categories["alimentacao"]
    assert review.new_kind == TransactionKind.EXPENSE


async def test_confirmar_a_sugestao_tambem_vira_revisao(
    api, app_tenant_session, categories, transaction_id
) -> None:
    """Confirmar não é no-op: é o exemplo negativo da medição.

    Uma tela que só registrasse discordância mediria a taxa de erro contra um
    denominador desconhecido.
    """
    await apply_llm_decision(
        app_tenant_session, transaction_id, categories["pix_enviado"], Decimal("0.950")
    )

    async with api() as client:
        await client.patch(
            f"/transactions/{transaction_id}",
            json={"category_id": str(categories["pix_enviado"])},
        )

    reviews = await fetch_reviews(app_tenant_session)
    assert len(reviews) == 1
    assert reviews[0].previous_category_id == reviews[0].new_category_id
    assert reviews[0].previous_confidence == Decimal("0.950")


async def test_cada_correcao_acrescenta_uma_linha(
    api, app_tenant_session, categories, transaction_id
) -> None:
    """Append-only: corrigir de novo não reescreve a revisão anterior."""
    async with api() as client:
        await client.patch(
            f"/transactions/{transaction_id}",
            json={"category_id": str(categories["alimentacao"])},
        )
        await client.patch(
            f"/transactions/{transaction_id}",
            json={"category_id": str(categories["pix_enviado"])},
        )

    reviews = await fetch_reviews(app_tenant_session)
    assert len(reviews) == 2
    # A segunda vê a primeira como estado anterior, já com origem MANUAL.
    assert reviews[1].previous_source == CategorySource.MANUAL
    assert reviews[1].previous_category_id == categories["alimentacao"]


# ---------------------------------------------------------------------------
# As recusas
# ---------------------------------------------------------------------------


async def test_categoria_inexistente_e_recusada(api, categories, transaction_id) -> None:
    async with api() as client:
        response = await client.patch(
            f"/transactions/{transaction_id}", json={"category_id": str(uuid.uuid4())}
        )

    assert response.status_code == 400


async def test_categoria_desativada_e_recusada(api, categories, transaction_id) -> None:
    """Aceitar traria de volta, pela porta dos fundos, uma categoria que o titular
    tirou de circulação — e que o LLM já não pode escolher."""
    async with api() as client:
        response = await client.patch(
            f"/transactions/{transaction_id}",
            json={"category_id": str(categories["desativada"])},
        )

    assert response.status_code == 400


async def test_transacao_inexistente_e_404(api, categories) -> None:
    async with api() as client:
        response = await client.patch(
            f"/transactions/{uuid.uuid4()}",
            json={"category_id": str(categories["alimentacao"])},
        )

    assert response.status_code == 404


async def test_reset_continua_recusando_manual(api, categories, transaction_id) -> None:
    """A contrapartida da tela: nada no sistema pode apagar a resposta do titular."""
    async with api() as client:
        await client.patch(
            f"/transactions/{transaction_id}",
            json={"category_id": str(categories["alimentacao"])},
        )
        response = await client.post("/categorization/reset", params={"source": "MANUAL"})

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# A correção contra o sync
# ---------------------------------------------------------------------------


@pytest.fixture
async def connection_a(admin_session: AsyncSession, tenants) -> uuid.UUID:
    tenant_a, _ = tenants
    connection_id = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO bank_connections (id, tenant_id, pluggy_item_id, status) "
            "VALUES (:id, :tenant_id, :item_id, 'UPDATING')"
        ),
        {"id": str(connection_id), "tenant_id": str(tenant_a), "item_id": str(ITEM_ID)},
    )
    await admin_session.commit()
    return connection_id


async def test_a_correcao_sobrevive_a_uma_re_sincronizacao(
    api, app_tenant_session, admin_session, categories, connection_a
) -> None:
    """A invariante 1 (`keep_if_decided`), agora pelo caminho da tela.

    `test_pluggy_sync.py` já cobre a preservação com um UPDATE escrito à mão. Aqui a
    correção vem do endpoint real — se o `PATCH` gravasse alguma coluna de um jeito
    que o upsert não reconhecesse, o histórico voltaria para a fila a cada coleta da
    Pluggy. Sem erro e sem log.
    """
    await sync_connection(
        tenant_id=TENANT_A,
        connection_id=connection_a,
        client=make_client(default_routes()),
        session_scope=app_tenant_session,
    )

    async with app_tenant_session(TENANT_A) as session:
        synced_id = await session.scalar(
            text("SELECT id FROM transactions ORDER BY external_id LIMIT 1")
        )

    async with api() as client:
        response = await client.patch(
            f"/transactions/{synced_id}",
            json={"category_id": str(categories["pix_enviado"]), "kind": "EXPENSE"},
        )
    assert response.status_code == 200

    # A Pluggy re-sincroniza insistindo no que ela acha.
    await sync_connection(
        tenant_id=TENANT_A,
        connection_id=connection_a,
        client=make_client(default_routes()),
        session_scope=app_tenant_session,
    )

    row = await fetch_transaction(app_tenant_session, synced_id)
    assert row.category_id == categories["pix_enviado"]
    assert row.category_source == CategorySource.MANUAL
    assert row.categorization_status == CategorizationStatus.CATEGORIZED
    # `kind` anda junto: revertê-lo devolveria o valor ao total de gastos sem
    # nenhum sinal de que algo mudou.
    assert row.kind == TransactionKind.EXPENSE
