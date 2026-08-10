"""Gravação de transações — deduplicação e upsert.

Contrapartida em código das duas constraints de unicidade do schema. Vive aqui, e
não dentro do cliente da Pluggy, porque as três origens de dado (Pluggy, OFX, CSV)
convergem nas mesmas duas regras: quem tem identificador estável faz upsert, quem
não tem é deduplicado por hash.

A Fase 2 (sync Pluggy) e a importação OFX/CSV consomem estas funções em vez de
escrever `INSERT` próprio.
"""

import hashlib
import re
import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import date as date_type
from decimal import Decimal
from typing import Any

from sqlalchemy import case
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import CategorySource, TransactionKind
from app.models.transaction import Transaction

_WHITESPACE = re.compile(r"\s+")


def normalize_description(description: str) -> str:
    """Forma canônica da descrição, usada só para calcular o hash de dedup.

    Caixa alta e espaços colapsados porque o mesmo lançamento costuma voltar do
    banco com espaçamento diferente entre uma exportação e outra — sem normalizar,
    a reimportação do mesmo extrato geraria hashes distintos e duplicaria tudo.

    Nunca sobrescreve `description_raw`: essa coluna é o insumo do LLM e da futura
    base de regras, e precisa continuar exatamente como veio da origem.
    """
    return _WHITESPACE.sub(" ", description).strip().upper()


def compute_dedup_hash(
    account_id: uuid.UUID,
    date: date_type,
    amount: Decimal,
    description: str,
) -> str:
    """Identidade sintética para origens sem identificador (CSV, entrada manual).

    O valor entra com escala fixa em duas casas para que `12.5` e `12.50` — a mesma
    quantia escrita de dois jeitos pelo exportador — produzam o mesmo hash.
    """
    payload = "|".join(
        [
            str(account_id),
            date.isoformat(),
            f"{Decimal(amount).quantize(Decimal('0.01')):f}",
            normalize_description(description),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assign_dedup_seq(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Numera as ocorrências repetidas do mesmo hash dentro do lote.

    É o que impede a constraint de descartar dado legítimo. Dois cafés de R$ 12,00
    no mesmo dia e no mesmo estabelecimento são duas transações reais, com hash
    idêntico; recebendo `dedup_seq` 0 e 1, ambas entram.

    E como a numeração segue a ordem do arquivo, reimportar o mesmo extrato produz
    exatamente os mesmos pares (hash, seq) — que colidem com o que já está gravado
    e são descartados pelo `ON CONFLICT DO NOTHING`. Determinístico, sem heurística
    de "parece duplicado".

    Limitação conhecida: se o mesmo café aparecer num segundo arquivo que também
    contenha o primeiro, o seq recomeça do zero e a nova ocorrência colide com a
    já gravada. É o comportamento certo para reimportação (o caso comum) e errado
    para extratos que se sobrepõem parcialmente. Resolver isso exige consultar o
    maior seq já gravado por hash — trabalho da Fase 2, quando houver dado real
    para calibrar.
    """
    seen: Counter[str] = Counter()
    result = []
    for row in rows:
        row = dict(row)
        dedup_hash = row["dedup_hash"]
        row["dedup_seq"] = seen[dedup_hash]
        seen[dedup_hash] += 1
        result.append(row)
    return result


def derive_kind(amount: Decimal) -> TransactionKind:
    """Natureza provisória de uma transação recém-ingerida, a partir do sinal.

    É só o ponto de partida: entrada vira INCOME, saída vira EXPENSE. Nenhuma
    origem de dado diz sozinha que um lançamento é transferência — isso depende
    de reconhecer que o destino é uma conta do próprio titular, ou que se trata do
    pagamento da fatura de um cartão que já está no sistema. Essa promoção para
    TRANSFER acontece quando a transação é categorizada, herdando o `kind` da
    categoria (Fase 2 em diante).

    O efeito prático é que uma transferência conta como gasto até ser
    classificada. O caminho oposto — assumir TRANSFER e corrigir depois —
    esconderia gasto real, que é o erro mais caro dos dois.
    """
    return TransactionKind.INCOME if amount >= 0 else TransactionKind.EXPENSE


def prepare_hashed_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Calcula `dedup_hash` e `dedup_seq` de um lote CSV/manual."""
    hashed = []
    for row in rows:
        row = dict(row)
        row["dedup_hash"] = compute_dedup_hash(
            account_id=row["account_id"],
            date=row["date"],
            amount=row["amount"],
            description=row["description_raw"],
        )
        row.setdefault("kind", derive_kind(row["amount"]))
        hashed.append(row)
    return assign_dedup_seq(hashed)


# Colunas que uma re-sincronização pode sobrescrever com segurança: são fatos
# reportados pela instituição, e a versão mais recente é sempre a correta.
_SYNCABLE_COLUMNS = (
    "amount",
    "currency_code",
    "date",
    "posted_at",
    "description_raw",
    "merchant",
    "pluggy_category_id",
    "pluggy_category_name",
    "raw_payload",
)


async def upsert_external_transactions(
    session: AsyncSession,
    rows: Sequence[dict[str, Any]],
) -> int:
    """Grava transações com identificador estável (Pluggy, OFX).

    Upsert e não insert porque transação da Pluggy **muda depois de criada**:
    PENDING vira POSTED, valor e descrição podem ser corrigidos pela instituição.
    Um `INSERT ... DO NOTHING` deixaria a primeira versão congelada no banco.

    O ponto sensível é a categoria. Quando o usuário corrige manualmente uma
    categorização, essa correção é o dado mais valioso do sistema — é a base de
    regras e de treino que as Fases 3 e 4 querem gerar. Uma re-sincronização
    sobrescrevendo-a com o palpite da Pluggy apagaria esse trabalho em silêncio,
    sem erro e sem log. Daí o CASE: linhas com `category_source = 'MANUAL'`
    preservam a categoria, e todo o resto é atualizado normalmente.
    """
    if not rows:
        return 0

    # `kind` é NOT NULL e sem default no banco. Derivar aqui, e não confiar em
    # cada chamador lembrar, é o que impede um sync novo de quebrar por omissão.
    rows = [{**row, "kind": row.get("kind") or derive_kind(row["amount"])} for row in rows]

    stmt = insert(Transaction).values(list(rows))

    # Dentro do ON CONFLICT, `Transaction.<col>` é o valor já gravado e
    # `stmt.excluded.<col>` é o que a sincronização trouxe.
    is_manual = Transaction.category_source == CategorySource.MANUAL

    def keep_if_manual(column: str):
        return case((is_manual, getattr(Transaction, column)), else_=stmt.excluded[column])

    # Só as colunas que o lote realmente traz. `excluded` expõe a tabela inteira,
    # então incluir uma coluna ausente do INSERT a sobrescreveria com o default
    # (normalmente NULL) — um sync parcial apagaria dado que ninguém pediu para apagar.
    present = set(rows[0].keys())
    syncable = [col for col in _SYNCABLE_COLUMNS if col in present]

    stmt = stmt.on_conflict_do_update(
        # Inferência do índice parcial: o predicado precisa ser repetido aqui,
        # senão o PostgreSQL não reconhece `uq_transactions_external` e levanta
        # "there is no unique or exclusion constraint matching the ON CONFLICT".
        index_elements=[Transaction.tenant_id, Transaction.source, Transaction.external_id],
        index_where=Transaction.external_id.isnot(None),
        set_={
            **{col: stmt.excluded[col] for col in syncable},
            # As colunas de categorização andam juntas: preservar só o
            # `category_id` deixaria a linha com categoria manual e `category_source`
            # apontando para a Pluggy, e a métrica de acerto da Fase 4 sairia errada.
            #
            # `kind` entra no grupo porque marcar um Pix como transferência é
            # decisão do usuário tanto quanto escolher a categoria — e um re-sync
            # revertendo-o para EXPENSE traria o valor de volta para o total de
            # gastos sem nenhum sinal de que algo mudou.
            "category_id": keep_if_manual("category_id"),
            "category_source": keep_if_manual("category_source"),
            "categorization_status": keep_if_manual("categorization_status"),
            "category_confidence": keep_if_manual("category_confidence"),
            "kind": keep_if_manual("kind"),
            # Ressuscita a linha. `deleted_at` marca "sumiu na origem"; se a
            # instituição voltou a reportar a transação, ela existe de novo e
            # esconder do extrato seria mostrar um saldo que não fecha.
            #
            # Isto vale enquanto o soft delete tiver um único significado. No dia
            # em que existir "excluir" na interface, essa exclusão precisa de
            # coluna própria — senão o primeiro re-sync desfaz o que o usuário
            # mandou apagar.
            "deleted_at": None,
        },
    )

    result = await session.execute(stmt)
    return result.rowcount or 0


async def insert_hashed_transactions(
    session: AsyncSession,
    rows: Sequence[dict[str, Any]],
) -> int:
    """Grava transações sem identificador (CSV, entrada manual).

    `DO NOTHING` e não `DO UPDATE`: sem identificador estável, uma colisão de
    (hash, seq) significa "esta linha já foi importada", não "esta linha mudou".
    Atualizar seria sobrescrever com dado idêntico e destruir a categorização.
    """
    if not rows:
        return 0

    stmt = insert(Transaction).values(list(rows))
    stmt = stmt.on_conflict_do_nothing(
        index_elements=[
            Transaction.tenant_id,
            Transaction.account_id,
            Transaction.dedup_hash,
            Transaction.dedup_seq,
        ],
        index_where=(Transaction.external_id.is_(None)) & (Transaction.deleted_at.is_(None)),
    )

    result = await session.execute(stmt)
    return result.rowcount or 0
