"""Normalização de texto para comparação. Pura.

Uma função só, usada por dois lados que precisam concordar: o sync, que compara o
nome do outro lado de um Pix com o do titular, e as regras do titular, que casam um
trecho com a descrição do lançamento. Se cada um normalizasse do seu jeito, "José"
casaria num e não no outro.
"""

import re
import unicodedata
from typing import Any

_SPACES = re.compile(r"\s+")


def fold(value: Any) -> str | None:
    """Caixa alta, sem acento, espaços colapsados: "José  da Silva" → "JOSE DA SILVA".

    Sem acento porque o mesmo nome chega com e sem ele conforme o banco que o
    escreveu. `None` para o que não é texto ou fica vazio.
    """
    if not isinstance(value, str):
        return None
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    folded = _SPACES.sub(" ", without_marks).strip().upper()
    return folded or None
