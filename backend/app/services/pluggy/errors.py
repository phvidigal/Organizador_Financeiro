"""Exceções da integração com a Pluggy.

A hierarquia existe para o sync poder decidir o que é falha e o que é limitação
conhecida. O caso que motiva a separação é `PluggyForbiddenError`: no tier
gratuito, disparar a atualização de um item pode ser recusado — e isso não é um
sync que falhou, é um sync que segue lendo o que a Pluggy já sincronizou.
"""

from typing import Any


class PluggyError(Exception):
    """Base de tudo que a integração levanta."""


class PluggyAuthError(PluggyError):
    """Credenciais recusadas, ou resposta de `/auth` num formato inesperado.

    Nunca carrega o segredo nem a chave: a mensagem cita apenas *quais* campos
    vieram na resposta, para que o log seja útil sem virar vazamento.
    """


class PluggyAPIError(PluggyError):
    """Resposta de erro da API, com status e corpo."""

    def __init__(self, status_code: int, path: str, body: Any = None) -> None:
        self.status_code = status_code
        self.path = path
        self.body = body
        super().__init__(f"Pluggy respondeu {status_code} em {path}: {body!r}")


class PluggyNotFoundError(PluggyAPIError):
    """404 — normalmente um `itemId` que não existe ou não é destas credenciais."""


class PluggyForbiddenError(PluggyAPIError):
    """403 — recurso existe mas o plano/credencial não autoriza a operação.

    É a resposta esperada do `PATCH /items/{id}` no tier pessoal. O sync trata
    como "atualização não suportada, siga em frente", não como erro.
    """


class PluggyUnavailableError(PluggyError):
    """Rede, timeout ou 5xx que sobreviveu às tentativas."""
