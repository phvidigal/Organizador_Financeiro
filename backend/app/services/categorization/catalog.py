"""A taxonomia do tenant, no formato que o LLM consegue consumir.

O modelo não escolhe um UUID: escolhe um **rótulo**. Este módulo é o de/para entre
os dois, e o único lugar que sabe como um rótulo é construído.

Rótulo é qualificado — `"Alimentação > Delivery"`, não `"Delivery"` — por duas
razões:

1. o índice único de categorias é `(tenant_id, parent_id, name)`, então dois pais
   podem ter filhas de mesmo nome. Um `enum` de nomes crus mapearia duas
   categorias diferentes na mesma string, e a resolução viraria chute;
2. a hierarquia chega ao modelo de graça, sem gastar uma linha de prompt
   explicando quem é filho de quem.

A parte pura (`build_catalog`) está separada da consulta (`load_catalog`) porque a
construção de rótulos e a resolução são o que precisa de teste, e testá-las não
deveria exigir banco.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category

logger = logging.getLogger(__name__)

LABEL_SEPARATOR = " > "


@dataclass(frozen=True)
class CategoryRow:
    """O que `build_catalog` precisa saber de uma categoria. Sem ORM, de propósito."""

    id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    kind: str
    pluggy_category_id: str | None = None


@dataclass(frozen=True)
class CategoryEntry:
    """Uma categoria já resolvida: rótulo, `kind` e a raiz da sua árvore."""

    id: uuid.UUID
    label: str
    kind: str
    root_id: uuid.UUID
    pluggy_category_id: str | None


@dataclass(frozen=True)
class CategoryCatalog:
    """Taxonomia de um tenant, indexada pelos três acessos que o job faz."""

    entries: tuple[CategoryEntry, ...] = ()
    by_label: dict[str, CategoryEntry] = field(default_factory=dict)
    by_id: dict[uuid.UUID, CategoryEntry] = field(default_factory=dict)
    # Contraparte local de uma categoria da Pluggy. Preenchido por
    # `sync_category_map`; é o que permite comparar as duas fontes.
    by_pluggy_category_id: dict[str, CategoryEntry] = field(default_factory=dict)

    @property
    def labels(self) -> list[str]:
        """Rótulos na ordem em que vão para o prompt e para o `enum` do schema."""
        return [entry.label for entry in self.entries]

    def __len__(self) -> int:
        return len(self.entries)

    def resolve(self, label: str) -> CategoryEntry | None:
        """Categoria de um rótulo devolvido pelo modelo, ou `None`.

        Tolera espaço em volta e diferença de caixa. O `enum` do schema já torna a
        resposta inválida improvável, mas a tolerância custa duas linhas e cobre o
        caso de um modelo que ignore o `format`.
        """
        entry = self.by_label.get(label)
        if entry is not None:
            return entry
        normalized = label.strip().casefold()
        return next((e for e in self.entries if e.label.casefold() == normalized), None)

    def pluggy_counterpart(self, pluggy_category_id: str | None) -> CategoryEntry | None:
        """A nossa categoria equivalente ao palpite da Pluggy, se houver de/para."""
        if not pluggy_category_id:
            return None
        return self.by_pluggy_category_id.get(pluggy_category_id)


def build_catalog(rows: Sequence[CategoryRow]) -> CategoryCatalog:
    """Monta o catálogo a partir das linhas de `categories`. Puro."""
    by_id_row = {row.id: row for row in rows}

    def ancestry(row: CategoryRow) -> list[CategoryRow]:
        """Do ancestral mais alto até a própria linha.

        O `seen` protege contra ciclo: nada no schema impede
        `A.parent_id = B AND B.parent_id = A`, e um ciclo aqui seria recursão
        infinita durante o carregamento do catálogo — o job morreria antes da
        primeira chamada ao Ollama, com um `RecursionError` que não diz nada sobre
        a causa.
        """
        chain: list[CategoryRow] = []
        seen: set[uuid.UUID] = set()
        current: CategoryRow | None = row
        while current is not None and current.id not in seen:
            seen.add(current.id)
            chain.append(current)
            current = by_id_row.get(current.parent_id) if current.parent_id else None
        chain.reverse()
        return chain

    entries: list[CategoryEntry] = []
    by_label: dict[str, CategoryEntry] = {}

    for row in rows:
        chain = ancestry(row)
        label = LABEL_SEPARATOR.join(link.name for link in chain)
        if label in by_label:
            # Impossível com a taxonomia de dois níveis atual, mas manter o
            # `enum` livre de rótulo ambíguo é o que garante que resolver a
            # resposta do modelo nunca vire escolha entre duas categorias.
            logger.warning("rótulo de categoria duplicado, ignorando a segunda: %r", label)
            continue
        entry = CategoryEntry(
            id=row.id,
            label=label,
            kind=row.kind,
            root_id=chain[0].id,
            pluggy_category_id=row.pluggy_category_id,
        )
        entries.append(entry)
        by_label[label] = entry

    entries.sort(key=lambda e: e.label)

    by_pluggy: dict[str, CategoryEntry] = {}
    for entry in entries:
        if entry.pluggy_category_id:
            # Uma categoria da Pluggy aponta para uma nossa (o schema tem uma coluna
            # só). Se duas nossas reivindicarem o mesmo id, o de/para está
            # inconsistente — fica com a primeira em ordem de rótulo, que é
            # determinístico, e avisa.
            if entry.pluggy_category_id in by_pluggy:
                logger.warning(
                    "pluggy_category_id %s mapeado por mais de uma categoria local",
                    entry.pluggy_category_id,
                )
                continue
            by_pluggy[entry.pluggy_category_id] = entry

    return CategoryCatalog(
        entries=tuple(entries),
        by_label=by_label,
        by_id={entry.id: entry for entry in entries},
        by_pluggy_category_id=by_pluggy,
    )


async def load_catalog(session: AsyncSession) -> CategoryCatalog:
    """Carrega a taxonomia do tenant corrente. O escopo vem do RLS, não de parâmetro.

    Só categorias ativas: uma categoria desativada pelo usuário não deve voltar a
    ser escolhida pelo LLM. As já gravadas continuam apontando para ela — desativar
    não é apagar.
    """
    result = await session.execute(
        select(
            Category.id,
            Category.parent_id,
            Category.name,
            Category.kind,
            Category.pluggy_category_id,
        ).where(Category.is_active.is_(True), Category.deleted_at.is_(None))
    )
    rows = [
        CategoryRow(
            id=row.id,
            parent_id=row.parent_id,
            name=row.name,
            kind=row.kind,
            pluggy_category_id=row.pluggy_category_id,
        )
        for row in result
    ]
    return build_catalog(rows)
