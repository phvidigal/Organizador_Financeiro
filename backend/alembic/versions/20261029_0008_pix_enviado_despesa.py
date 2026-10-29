"""Move "Pix enviado" de `Transferências` para `Outros` — vira despesa.

O espelho da `0004`. `Pix enviado` nasceu sob `Transferências`, que é TRANSFER, e
isso embutia a premissa de que todo Pix sem motivo conhecido era dinheiro do
titular mudando de conta. Não é: pagar alguém por um serviço é gasto, e o que
ficava em `Pix enviado` saía dos totais calado — o erro que a invariante 4 manda
evitar, porque esconde gasto real.

O caso legítimo de transferência deixou de depender desta categoria. Desde a
`0007`, a conta própria é reconhecida por evidência (`counterparty_is_holder`) e
vai para `Transferências > Transferência entre contas próprias`. O que sobra em
`Pix enviado` é pagamento cujo motivo a descrição não diz — e quem sabe o motivo
é o titular, por isso `decide.decide` manda toda escolha desta categoria para a
revisão, com a pergunta "com o que foi gasto este Pix?".

As transações que apontam para ela recebem o mesmo tratamento da `0004`:

* **decisão automática** volta para a fila: o rótulo mudou de significado, e a
  escolha feita sob o antigo precisa ser refeita, não remendada;
* **correção manual** é preservada e herda o `kind` novo (invariante 1b).

Mesmo id de antes, embora o `uuid5` tenha sido derivado do pai antigo: o id é
identidade, não endereço — mesma ressalva da `0004`.

Revision ID: 0008_pix_enviado_despesa
Revises: 0007_counterparty_and_rule
Create Date: 2026-10-29
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_pix_enviado_despesa"
down_revision: str | None = "0007_counterparty_and_rule"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATEGORY_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def category_uuid(tenant_id: uuid.UUID, parent: str | None, name: str) -> uuid.UUID:
    return uuid.uuid5(CATEGORY_NAMESPACE, f"{tenant_id}:{parent or ''}:{name}")


PIX_ENVIADO = category_uuid(DEFAULT_TENANT_ID, "Transferências", "Pix enviado")
OUTROS = category_uuid(DEFAULT_TENANT_ID, None, "Outros")
TRANSFERENCIAS = category_uuid(DEFAULT_TENANT_ID, None, "Transferências")


def _move(new_parent: uuid.UUID, new_kind: str) -> None:
    """Reparenta a categoria e reconcilia as transações. Ver o docstring do módulo."""
    conn = op.get_bind()

    conn.execute(
        sa.text("UPDATE categories SET parent_id = :p, kind = :k WHERE id = :id"),
        {"p": str(new_parent), "k": new_kind, "id": str(PIX_ENVIADO)},
    )

    conn.execute(
        sa.text(
            "UPDATE transactions SET kind = :k "
            "WHERE category_id = :id AND category_source = 'MANUAL'"
        ),
        {"k": new_kind, "id": str(PIX_ENVIADO)},
    )

    conn.execute(
        sa.text(
            "UPDATE transactions SET "
            "  category_id = NULL, "
            "  category_source = NULL, "
            "  category_rule_id = NULL, "
            "  categorization_status = 'PENDING', "
            "  category_confidence = NULL, "
            "  categorized_at = NULL, "
            "  kind = CASE WHEN amount >= 0 THEN 'INCOME' ELSE 'EXPENSE' END "
            "WHERE category_id = :id AND category_source IS DISTINCT FROM 'MANUAL'"
        ),
        {"id": str(PIX_ENVIADO)},
    )


def upgrade() -> None:
    _move(OUTROS, "EXPENSE")


def downgrade() -> None:
    _move(TRANSFERENCIAS, "TRANSFER")
