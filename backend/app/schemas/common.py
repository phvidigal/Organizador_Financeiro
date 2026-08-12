"""Tipos compartilhados pelos schemas de resposta."""

from decimal import Decimal
from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, PlainSerializer

# Dinheiro atravessa a API como **string**, nunca como número JSON.
#
# A invariante "dinheiro é NUMERIC(18,2), nunca float" não pode parar no banco:
# `JSON.parse` do navegador transforma número em double, e 0,10 volta a não ser
# representável — exatamente o erro que o `NUMERIC` existe para evitar. String
# atravessa a fronteira intacta, e o frontend converte só na hora de formatar.
#
# Duas casas sempre, inclusive em valores redondos: `"1500.00"` e não `"1500"`,
# para que o cliente não precise adivinhar a escala.
Money = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.2f}", return_type=str)]

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """Página de resultados com o total, para a interface saber quando parar."""

    items: list[T]
    total: int
    limit: int
    offset: int
