"""Schema de categoria.

Espelha `CategoryEntry` de `app/services/categorization/catalog.py`, e não o modelo
`Category`, de propósito: é o mesmo catálogo que vai para o `enum` do JSON Schema
do Ollama. **O humano escolhe da mesma lista que o LLM vê** — se a taxonomia
oferecida ao titular divergisse da oferecida ao modelo, a correção manual deixaria
de ser régua comparável, que é a razão de a tela de revisão existir.
"""

import uuid

from pydantic import BaseModel


class CategoryRead(BaseModel):
    id: uuid.UUID

    # Rótulo qualificado: "Alimentação > Delivery", não "Delivery". O índice único
    # é `(tenant_id, parent_id, name)`, então nome cru pode ser ambíguo entre dois
    # pais — e a hierarquia chega à interface sem um segundo campo.
    label: str

    # INCOME / EXPENSE / TRANSFER. A transação herda este valor ao ser categorizada,
    # e é o default que a tela de revisão oferece — sobreponível pelo titular.
    kind: str

    # Raiz da árvore desta categoria. Serve para agrupar o seletor por `<optgroup>`
    # sem o cliente ter de reconstruir a hierarquia a partir do rótulo.
    root_id: uuid.UUID

    # Contraparte na taxonomia da Pluggy, quando há de/para. É o que permite à tela
    # mostrar o palpite do agregador ao lado do do LLM.
    pluggy_category_id: str | None
