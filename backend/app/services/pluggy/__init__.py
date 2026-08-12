"""Integração com a Pluggy — três camadas com fronteira rígida.

* `client`  — fala HTTP e nada mais. Não conhece SQLAlchemy.
* `mappers` — funções puras que traduzem JSON da Pluggy em dicts de coluna.
              Não conhecem HTTP nem banco.
* `sync`    — orquestra as duas anteriores e grava via `app.services.ingestion`.

A separação não é cerimônia: é o que permite testar o mapeamento sem rede nem
banco, e é o que mantém a convenção de sinal do cartão de crédito num único
arquivo sem I/O, para ser corrigida com uma constante quando o dado real chegar.
"""

from app.services.pluggy.client import PluggyClient
from app.services.pluggy.errors import (
    PluggyAPIError,
    PluggyAuthError,
    PluggyError,
    PluggyForbiddenError,
    PluggyNotFoundError,
    PluggyUnavailableError,
)

__all__ = [
    "PluggyAPIError",
    "PluggyAuthError",
    "PluggyClient",
    "PluggyError",
    "PluggyForbiddenError",
    "PluggyNotFoundError",
    "PluggyUnavailableError",
]
