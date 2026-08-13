"""Move "Pix recebido" de `Transferências` para `Receitas`.

Buraco medido na primeira rodada completa da Fase 3: `Receitas` ficou com 13
lançamentos — só rendimento de investimento, menos de 1% do dinheiro que de fato
entrou no período. Os outros 99% estavam em `Transferências > Pix recebido`, em 34
lançamentos com parcelas mensais de valor idêntico e pagamentos vindos de CNPJ.
Isso é renda, e estava fora dos totais.

A causa não foi o modelo: foi a taxonomia da Fase 1. `Pix recebido` nasceu sob
`Transferências`, que é TRANSFER, então **todo** Pix recebido era tratado como
dinheiro andando entre contas do próprio titular. A premissa é falsa para qualquer
pessoa que receba pagamento por Pix no Brasil.

O caso legítimo de transferência entre contas próprias continua tendo casa —
`Transferências > Transferência entre contas próprias`, que já existia. A escolha
entre as duas passa a ser uma decisão de conteúdo, e é isso que se quer: quando a
descrição não permitir decidir, o prompt manda o modelo baixar a confiança, a
transação cai em `NEEDS_REVIEW` e quem responde é o titular. A resposta vira
`category_source = 'MANUAL'`, que é a base da pipeline híbrida.

**Por que UPDATE e não DELETE + INSERT.** O id determinístico continua sendo
`uuid5(..., "tenant:Transferências:Pix recebido")`, que passa a não descrever mais
o pai atual. É feio e é de propósito: recriar a linha com o id "certo" faria a FK
`ON DELETE SET NULL` zerar `category_id` de toda transação que apontasse para ela,
inclusive as corrigidas à mão. Um id com derivação histórica custa uma nota de
rodapé; perder correção do usuário custa o dado mais caro do sistema.

Revision ID: 0004_pix_recebido_receita
Revises: 0003_investment_categories
Create Date: 2026-08-13
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_pix_recebido_receita"
down_revision: str | None = "0003_investment_categories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATEGORY_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def category_uuid(tenant_id: uuid.UUID, parent: str | None, name: str) -> uuid.UUID:
    return uuid.uuid5(CATEGORY_NAMESPACE, f"{tenant_id}:{parent or ''}:{name}")


PIX_RECEBIDO = category_uuid(DEFAULT_TENANT_ID, "Transferências", "Pix recebido")
RECEITAS = category_uuid(DEFAULT_TENANT_ID, None, "Receitas")
TRANSFERENCIAS = category_uuid(DEFAULT_TENANT_ID, None, "Transferências")


def _move(new_parent: uuid.UUID, new_kind: str) -> None:
    """Reparenta a categoria e reconcilia as transações que apontam para ela.

    Duas populações, tratadas de forma diferente:

    * **decisão automática** (`category_source` diferente de `MANUAL`) volta para a
      fila. O rótulo mudou de significado — quem escolheu "Pix recebido" achando
      que era transferência não escolheu o que a categoria diz agora —, então a
      decisão precisa ser refeita, não remendada;
    * **correção manual** é preservada e só herda o `kind` novo. O titular escolheu
      esta categoria sabendo o que estava fazendo; o que mudou foi a natureza dela,
      e `kind` herdado de `categories.kind` é a invariante 1b.
    """
    conn = op.get_bind()

    conn.execute(
        sa.text("UPDATE categories SET parent_id = :p, kind = :k WHERE id = :id"),
        {"p": str(new_parent), "k": new_kind, "id": str(PIX_RECEBIDO)},
    )

    conn.execute(
        sa.text(
            "UPDATE transactions SET kind = :k "
            "WHERE category_id = :id AND category_source = 'MANUAL'"
        ),
        {"k": new_kind, "id": str(PIX_RECEBIDO)},
    )

    conn.execute(
        sa.text(
            "UPDATE transactions SET "
            "  category_id = NULL, "
            "  category_source = NULL, "
            "  categorization_status = 'PENDING', "
            "  category_confidence = NULL, "
            "  categorized_at = NULL, "
            "  kind = CASE WHEN amount >= 0 THEN 'INCOME' ELSE 'EXPENSE' END "
            "WHERE category_id = :id AND category_source IS DISTINCT FROM 'MANUAL'"
        ),
        {"id": str(PIX_RECEBIDO)},
    )


def upgrade() -> None:
    _move(RECEITAS, "INCOME")


def downgrade() -> None:
    _move(TRANSFERENCIAS, "TRANSFER")
