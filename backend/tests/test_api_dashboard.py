"""Resumo do dashboard: o que pode ser somado junto e o que não pode.

A regressão mais cara desta tela é silenciosa: se `TRANSFER` vazar para `expense`,
o total continua parecendo plausível — só está errado. Um mês com R$ 5.000
aplicados num CDB apareceria como R$ 5.000 de gasto, e o resgate dos mesmos
R$ 5.000, meses depois, como receita. É o mesmo dinheiro contado duas vezes, e é
precisamente o que o campo `kind` existe para evitar.

O cenário é montado à mão, com valores redondos, para que cada asserção seja uma
conta que dá para conferir de cabeça.
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
from app.services.dashboard import INACTIVE_CATEGORY, NO_CATEGORY
from app.services.ingestion import upsert_external_transactions
from tests.conftest import TENANT_A

pytestmark = pytest.mark.asyncio(loop_scope="session")

PERIODO = {"date_from": "2026-06-01", "date_to": "2026-07-31"}


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
    tenant_a, _ = tenants
    ids = {"salario": uuid.uuid4(), "mercado": uuid.uuid4(), "transferencias": uuid.uuid4()}
    await admin_session.execute(
        text(
            "INSERT INTO categories (id, tenant_id, name, kind) VALUES "
            "(:s, :t, 'Salário',        'INCOME'), "
            "(:m, :t, 'Mercado',        'EXPENSE'), "
            "(:x, :t, 'Transferências', 'TRANSFER')"
        ),
        {
            "t": str(tenant_a),
            "s": str(ids["salario"]),
            "m": str(ids["mercado"]),
            "x": str(ids["transferencias"]),
        },
    )
    await admin_session.commit()
    return ids


@pytest.fixture
async def account_b(admin_session: AsyncSession, tenants) -> uuid.UUID:
    """Segunda conta, só para o filtro por conta ter o que excluir."""
    tenant_a, _ = tenants
    account_id = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO accounts (id, tenant_id, type, subtype, name) "
            "VALUES (:id, :t, 'CREDIT', 'CREDIT_CARD', 'Cartão')"
        ),
        {"id": str(account_id), "t": str(tenant_a)},
    )
    await admin_session.commit()
    return account_id


@pytest.fixture
async def movimento(app_tenant_session, tenants, account_a, account_b, categories):
    """Junho e julho de 2026, com um caso de cada coisa que a tela precisa acertar.

    Somas esperadas no período completo, **sem filtro de conta** (que é como a tela
    abre):

        receita   +1.200,00  (2 lançamentos, 200,00 em revisão)
        despesa     -477,00  (3 lançamentos, 100,00 em revisão)
        transfer    -500,00  (1 lançamento, nada em revisão)
        saldo       +723,00  ← transferência **fora**

    A despesa de −77,00 mora na segunda conta e cai na mesma categoria da de
    −300,00: é o que prova que a quebra agrupa por categoria e não por conta.
    """
    tenant_a, _ = tenants

    def linha(external_id, amount, kind, category_id, status, dia, account=account_a):
        return {
            "tenant_id": tenant_a,
            "account_id": account,
            "source": "PLUGGY",
            "external_id": external_id,
            "amount": Decimal(amount),
            "currency_code": "BRL",
            "date": dia,
            "description_raw": external_id,
            "kind": kind,
            "category_id": category_id,
            "categorization_status": status,
        }

    rows = [
        linha("jun-salario", "1000.00", "INCOME", categories["salario"],
              "CATEGORIZED", date(2026, 6, 10)),
        linha("jun-mercado", "-300.00", "EXPENSE", categories["mercado"],
              "CATEGORIZED", date(2026, 6, 15)),
        linha("jun-pix", "-500.00", "TRANSFER", categories["transferencias"],
              "CATEGORIZED", date(2026, 6, 20)),
        linha("jul-salario", "200.00", "INCOME", categories["salario"],
              "NEEDS_REVIEW", date(2026, 7, 5)),
        # Sem categoria: precisa aparecer num balde próprio, não sumir da soma.
        linha("jul-sem-categoria", "-100.00", "EXPENSE", None,
              "NEEDS_REVIEW", date(2026, 7, 10)),
        # Excluída na origem: fora de tudo.
        linha("jul-apagada", "-9999.00", "EXPENSE", categories["mercado"],
              "CATEGORIZED", date(2026, 7, 20)),
        # Outra conta: só o filtro por conta a exclui.
        linha("jul-cartao", "-77.00", "EXPENSE", categories["mercado"],
              "CATEGORIZED", date(2026, 7, 25), account=account_b),
    ]

    async with app_tenant_session(tenant_a) as session:
        await upsert_external_transactions(session, rows)

    async with app_tenant_session(tenant_a) as session:
        await session.execute(
            text("UPDATE transactions SET deleted_at = now() WHERE external_id = 'jul-apagada'")
        )


async def get_summary(api, **params):
    async with api() as client:
        response = await client.get("/dashboard/summary", params={**PERIODO, **params})
    assert response.status_code == 200
    return response.json()


# ---------------------------------------------------------------------------
# A separação que dá sentido ao resto
# ---------------------------------------------------------------------------


async def test_transferencia_fica_fora_do_saldo(api, movimento) -> None:
    """A regressão silenciosa desta tela.

    R$ 500 de transferência somados à despesa dariam −R$ 977 e um saldo de R$ 223 —
    plausível, e errado. O dinheiro não saiu do titular, só mudou de lugar.
    """
    body = await get_summary(api)

    assert body["income"]["total"] == "1200.00"
    assert body["expense"]["total"] == "-477.00"
    assert body["transfer"]["total"] == "-500.00"
    # 1200 − 477, com a transferência de fora.
    assert body["net"] == "723.00"


async def test_sinal_e_escala_atravessam_a_api(api, movimento) -> None:
    """Dinheiro é string de duas casas, e despesa é negativa como está no banco.

    Número JSON viraria double no `JSON.parse` do navegador, e 0,10 voltaria a não
    ser representável — o erro que o `NUMERIC(18,2)` existe para evitar.
    """
    body = await get_summary(api)

    for valor in (body["net"], body["income"]["total"], body["expense"]["total"]):
        assert isinstance(valor, str)
        assert valor.split(".")[-1] != "" and len(valor.split(".")[-1]) == 2

    assert body["expense"]["total"].startswith("-")


# ---------------------------------------------------------------------------
# A fatia sob revisão
# ---------------------------------------------------------------------------


async def test_cada_kind_carrega_quanto_ainda_falta_confirmar(api, movimento) -> None:
    """Sem isto, quem lê o total não sabe quanto dele é palpite esperando resposta.

    No banco real hoje isso não é detalhe: 99% da receita está em NEEDS_REVIEW,
    porque o modelo baixa a confiança quando só o titular sabe quem mandou o Pix.
    """
    body = await get_summary(api)

    assert body["income"]["needs_review_total"] == "200.00"
    assert body["income"]["needs_review_count"] == 1
    assert body["expense"]["needs_review_total"] == "-100.00"
    assert body["expense"]["needs_review_count"] == 1
    # A transferência foi decidida com confiança; nada dela está na fila.
    assert body["transfer"]["needs_review_total"] == "0.00"
    assert body["transfer"]["needs_review_count"] == 0

    assert body["queue"]["needs_review"] == 2
    # Quatro decididas; a excluída na origem não conta.
    assert body["queue"]["categorized"] == 4


# ---------------------------------------------------------------------------
# Quebra por categoria
# ---------------------------------------------------------------------------


async def test_transacao_sem_categoria_vira_balde_proprio(api, movimento) -> None:
    """Some da quebra, mas não da soma — e a discrepância seria inexplicável."""
    body = await get_summary(api)

    sem_categoria = [c for c in body["by_category"] if c["category_id"] is None]
    assert len(sem_categoria) == 1
    assert sem_categoria[0]["label"] == NO_CATEGORY
    assert sem_categoria[0]["total"] == "-100.00"

    # A soma da quebra fecha com os totais dos três kind.
    total_quebra = sum(Decimal(c["total"]) for c in body["by_category"])
    assert total_quebra == Decimal("1200.00") - Decimal("477.00") - Decimal("500.00")


async def test_categoria_desativada_nao_vira_nome_inventado(
    api, admin_session, movimento, categories
) -> None:
    """`load_catalog` só devolve categorias ativas — a transação já gravada continua
    apontando para ela, e o rótulo precisa dizer isso em vez de sumir."""
    await admin_session.execute(
        text("UPDATE categories SET is_active = false WHERE id = :id"),
        {"id": str(categories["mercado"])},
    )
    await admin_session.commit()

    body = await get_summary(api)

    inativa = [c for c in body["by_category"] if c["label"] == INACTIVE_CATEGORY]
    assert len(inativa) == 1
    # −300,00 da conta corrente e −77,00 do cartão, no mesmo balde: a quebra agrupa
    # por categoria, não por conta.
    assert inativa[0]["total"] == "-377.00"


async def test_quebra_ordena_pelo_peso_absoluto(api, movimento) -> None:
    """Despesa é negativa e receita positiva: ordenar pelo número cru misturaria as
    duas pontas e deixaria o maior gasto no fim da lista."""
    body = await get_summary(api)

    pesos = [abs(Decimal(c["total"])) for c in body["by_category"]]
    assert pesos == sorted(pesos, reverse=True)


# ---------------------------------------------------------------------------
# Recortes
# ---------------------------------------------------------------------------


async def test_serie_mensal_separa_os_tres_kind(api, movimento) -> None:
    body = await get_summary(api)

    meses = {m["month"]: m for m in body["by_month"]}
    assert list(meses) == ["2026-06", "2026-07"]

    assert meses["2026-06"]["income"] == "1000.00"
    assert meses["2026-06"]["expense"] == "-300.00"
    assert meses["2026-06"]["transfer"] == "-500.00"

    assert meses["2026-07"]["income"] == "200.00"
    # −100,00 sem categoria mais −77,00 do cartão.
    assert meses["2026-07"]["expense"] == "-177.00"
    # Mês sem transferência nenhuma continua devolvendo zero, e não ausência.
    assert meses["2026-07"]["transfer"] == "0.00"


async def test_filtro_de_periodo_recorta(api, movimento) -> None:
    body = await get_summary(api, date_from="2026-07-01")

    assert body["income"]["total"] == "200.00"
    assert body["expense"]["total"] == "-177.00"
    assert body["transfer"]["total"] == "0.00"
    assert [m["month"] for m in body["by_month"]] == ["2026-07"]


async def test_filtro_de_conta_recorta(api, movimento, account_b) -> None:
    """O lançamento do cartão só entra quando a conta dele é a pedida."""
    todas = await get_summary(api)
    assert todas["expense"]["total"] == "-477.00"

    corrente = await get_summary(api, account_id=str(account_b))
    assert corrente["expense"]["total"] == "-77.00"
    assert corrente["income"]["total"] == "0.00"


async def test_linha_excluida_fica_fora_de_tudo(api, movimento) -> None:
    """`deleted_at` significa "sumiu na origem". R$ 9.999 de despesa fantasma
    apareceriam de imediato se o filtro faltasse."""
    body = await get_summary(api)

    assert body["expense"]["total"] == "-477.00"
    assert all(Decimal(c["total"]) > Decimal("-9000") for c in body["by_category"])
