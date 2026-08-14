"""Registro append-only de cada correção manual de categorização.

Existe porque a correção **destrói o dado que ela produz**. Gravar
`category_source = 'MANUAL'` sobrescreve `category_id` e `category_confidence`, e
com eles some o par que a Fase 3 deixou explicitamente pendente: *"se `0.450` erra
mais que `0.950`, só as correções `MANUAL` dirão"* (`docs/fases-3-5.md`).

Depois de sobrescrever não dá nem para saber se o titular **confirmou** a escolha
do LLM ou a **corrigiu** — que é exatamente a distinção a ser medida. Esta tabela
guarda o estado anterior antes de cada gravação, então a medição vira uma consulta:

    SELECT previous_confidence,
           previous_category_id = new_category_id AS acertou
    FROM categorization_reviews;

É também o insumo natural da pipeline híbrida (`regra → embedding → LLM`) da Fase
5: uma regra aprendida sai de "descrição X foi corrigida para categoria Y", e isso
é uma linha daqui, não uma coluna de `transactions`.

**Append-only por privilégio, não por convenção.** A migration concede
`SELECT, INSERT, DELETE` ao `app_user` e omite `UPDATE`: um caminho de código que
tente reescrever uma revisão falha no banco. O `DELETE` fica porque a eliminação do
titular (LGPD art. 18, VI) é `DELETE FROM tenants` em cascata.

Sem `TimestampMixin` e sem `SoftDeleteMixin` pelo mesmo motivo: linha de log não é
atualizada nem excluída logicamente, então `updated_at` e `deleted_at` seriam
colunas que nunca mudam de valor — e `updated_at` ainda arrastaria junto a trigger.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Enum, ForeignKey, Index, Numeric, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.models.base import Base, tenant_fk_column, uuid_pk
from app.models.enums import CategorizationStatus, CategorySource, TransactionKind


class CategorizationReview(Base):
    __tablename__ = "categorization_reviews"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk_column()

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=False,
    )

    # --- Estado anterior -----------------------------------------------------
    #
    # Tudo nulo é possível e legítimo: é o caso da transação que nunca passou pelo
    # LLM (`PENDING`) e foi categorizada direto pelo titular no extrato.
    #
    # `ON DELETE SET NULL` na categoria anterior, e não CASCADE: apagar uma
    # categoria não pode apagar a evidência de que ela foi escolhida um dia — seria
    # perder o histórico justamente das categorias que se mostraram ruins.
    previous_category_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Os `name=` são únicos dentro da tabela, e não os `transaction_kind` /
    # `category_source` usados em `transactions`: a convenção de nomes monta
    # `ck_<tabela>_<name>`, e duas colunas com o mesmo `name` na mesma tabela
    # colidiriam num único nome de constraint.
    previous_kind: Mapped[str | None] = mapped_column(
        Enum(TransactionKind, native_enum=False, length=16, name="previous_kind"),
        nullable=True,
    )
    previous_source: Mapped[str | None] = mapped_column(
        Enum(CategorySource, native_enum=False, length=16, name="previous_source"),
        nullable=True,
    )
    previous_status: Mapped[str | None] = mapped_column(
        Enum(CategorizationStatus, native_enum=False, length=24, name="previous_status"),
        nullable=True,
    )
    # A confiança **crua** do modelo, na escala em que ele a declarou. É a coluna
    # que responde à pergunta da calibração; `transactions.category_confidence` é
    # zerada na correção justamente porque o número passa a morar aqui.
    previous_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)

    # --- Resposta do titular -------------------------------------------------
    #
    # Sempre preenchida na escrita, mas anulável na coluna: o `SET NULL` da FK
    # precisa de espaço para agir. Uma categoria apagada não pode levar embora a
    # linha que registra que ela foi escolhida.
    new_category_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    new_kind: Mapped[str] = mapped_column(
        Enum(TransactionKind, native_enum=False, length=16, name="new_kind"),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# Todas as revisões de uma transação, para a tela mostrar "corrigida antes".
#
# Também é o índice da FK de `transaction_id`: o Postgres não cria índice de chave
# estrangeira sozinho, e `transactions` é a tabela de onde o CASCADE parte. Sem
# ele, apagar uma transação faria seq scan aqui.
Index(
    "ix_categorization_reviews_transaction_id",
    CategorizationReview.transaction_id,
    CategorizationReview.tenant_id,
)

# A leitura da medição: as revisões do tenant, mais recentes primeiro.
Index(
    "ix_categorization_reviews_tenant_created",
    CategorizationReview.tenant_id,
    CategorizationReview.created_at.desc(),
)

# As duas FKs de categoria ficam **sem** índice, ao contrário da convenção do resto
# do schema. É deliberado: o caminho que elas protegeriam é `DELETE FROM categories`
# (ou o `SET NULL` correspondente), que não acontece em operação normal — categoria
# se desativa (`is_active`), não se apaga. E esta tabela cresce uma linha por
# correção humana, não uma por transação sincronizada.

