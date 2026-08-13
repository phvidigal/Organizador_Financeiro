"""Árvore "Investimentos", com `kind = TRANSFER`.

Buraco descoberto na primeira rodada real da Fase 3: "Compra de Renda Variável"
caiu em `Outros` porque a taxonomia só tinha `Receitas > Rendimentos e
investimentos`, que é INCOME. O LLM não errou — não havia para onde ir. No extrato
real são 12 lançamentos ("Compra de Renda Variável" ×10, "Aplicação em CDB",
"Aplicação RDB"), todos contando como despesa.

**Por que TRANSFER e não EXPENSE.** Comprar um CDB não é consumo: o dinheiro
continua sendo do titular, só mudou de conta. Um mês em que se aplicou R$ 5.000
apareceria como R$ 5.000 de gasto, e o resgate desses mesmos R$ 5.000 apareceria
depois como receita — o mesmo dinheiro contado duas vezes, que é exatamente o erro
que o campo `kind` existe para evitar. É o mesmo raciocínio de "Pagamento de
cartão".

**O rendimento continua sendo receita, e continua onde está.** `Receitas >
Rendimentos e investimentos` (INCOME) é para juros e dividendos — dinheiro novo. A
árvore criada aqui é para a movimentação do principal. No extrato real a diferença
é visível: "Valor recebido de Investimentos" (13 lançamentos, positivos) é
rendimento; "Compra de Renda Variável" é principal saindo.

**A direção mora no sinal**, como em "Transferências": aplicação é negativa,
resgate é positivo, e a categoria é a mesma. Duplicar a árvore em
aplicação/resgate dobraria o `enum` do prompt para distinguir o que o sinal já diz.

Revision ID: 0003_investment_categories
Revises: 0002_next_auto_sync_at
Create Date: 2026-08-13
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_investment_categories"
down_revision: str | None = "0002_next_auto_sync_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Os mesmos da migration inicial: os ids precisam continuar determinísticos, senão
# recriar o banco produz ids diferentes e dumps deixam de ser comparáveis.
CATEGORY_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

ROOT = "Investimentos"
KIND = "TRANSFER"

# Separado por classe de ativo, que é o corte que a descrição do extrato sustenta
# ("Compra de Renda Variável", "Aplicação em CDB"). Cortes mais finos — por
# emissor, por corretora — o modelo não teria como inferir, e cada rótulo a mais
# entra no `enum` de toda chamada.
CHILDREN = [
    "Renda fixa",
    "Renda variável",
    "Fundos de investimento",
    "Criptoativos",
    "Previdência privada",
]


def category_uuid(tenant_id: uuid.UUID, parent: str | None, name: str) -> uuid.UUID:
    return uuid.uuid5(CATEGORY_NAMESPACE, f"{tenant_id}:{parent or ''}:{name}")


def _ids() -> tuple[uuid.UUID, list[uuid.UUID]]:
    root_id = category_uuid(DEFAULT_TENANT_ID, None, ROOT)
    return root_id, [category_uuid(DEFAULT_TENANT_ID, ROOT, name) for name in CHILDREN]


def upgrade() -> None:
    conn = op.get_bind()
    root_id, child_ids = _ids()

    rows = [{"id": str(root_id), "parent_id": None, "name": ROOT}]
    rows += [
        {"id": str(child_id), "parent_id": str(root_id), "name": name}
        for child_id, name in zip(child_ids, CHILDREN, strict=True)
    ]

    # `pluggy_category_id` fica NULL: o de/para é preenchido em runtime por
    # `sync_category_map`, a partir de `GET /categories`. Chutar o id aqui criaria
    # um mapeamento errado que ninguém notaria até conferir uma categorização.
    conn.execute(
        sa.text(
            "INSERT INTO categories (id, tenant_id, parent_id, name, kind, is_system) "
            "VALUES (:id, :tenant_id, :parent_id, :name, :kind, true)"
        ),
        [
            {**row, "tenant_id": str(DEFAULT_TENANT_ID), "kind": KIND}
            for row in rows
        ],
    )


def downgrade() -> None:
    """Devolve à fila o que apontava para estas categorias, e só então apaga.

    A FK de `transactions.category_id` é `ON DELETE SET NULL`: apagar sem mais nada
    deixaria linhas com `categorization_status = 'CATEGORIZED'`, `kind = 'TRANSFER'`
    e categoria nenhuma — um estado que nenhum código do sistema produz e que o
    dashboard leria como transferência sem destino.

    `MANUAL` também volta para a fila aqui. É a única exceção à regra de nunca
    apagar correção do usuário, e ela se justifica: a categoria que o usuário
    escolheu está deixando de existir, então preservar o `category_source` apontando
    para o nada seria pior.
    """
    conn = op.get_bind()
    root_id, child_ids = _ids()
    all_ids = [str(root_id), *(str(cid) for cid in child_ids)]

    conn.execute(
        sa.text(
            "UPDATE transactions SET "
            "  category_id = NULL, "
            "  category_source = NULL, "
            "  categorization_status = 'PENDING', "
            "  category_confidence = NULL, "
            "  categorized_at = NULL, "
            "  kind = CASE WHEN amount >= 0 THEN 'INCOME' ELSE 'EXPENSE' END "
            "WHERE category_id = ANY(:ids)"
        ),
        {"ids": all_ids},
    )

    conn.execute(
        sa.text("DELETE FROM categories WHERE id = ANY(:ids)"),
        {"ids": all_ids},
    )
