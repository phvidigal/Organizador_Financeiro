"""Defesas de borda de um app que ainda não tem autenticação.

Sem login, quem alcança a API lê e altera o extrato inteiro. Três camadas mantêm
esse "quem" restrito ao titular, na própria máquina:

1. **A porta só escuta em 127.0.0.1** (`docker-compose.yml`). Nenhum outro
   aparelho da rede chega aqui.
2. **O `Host` precisa ser local** (`TrustedHostMiddleware`, montado em
   `app/main.py` com `Settings.allowed_hosts`). Barra DNS rebinding: uma página
   que faça o próprio domínio resolver para 127.0.0.1 chega com `Host: dominio-dela`
   e é recusada antes de qualquer rota — inclusive as de leitura, que o CORS não
   protege nesse cenário, porque para o navegador a requisição é same-origin.
3. **Escrita vinda de outra origem é recusada** (`OriginCheckMiddleware`, abaixo).

A terceira existe porque o CORS bloqueia a *leitura* da resposta, não a
*execução*. Um `POST /categorization/reset` disparado por um formulário de outro
site é uma "requisição simples", sem preflight: chega ao handler, apaga as
decisões do LLM, e só a resposta é escondida de quem disparou.

Nenhuma substitui autenticação. Elas valem enquanto o app for só local — e
`Settings._hosts_are_local` impede o boot no dia em que alguém tentar deixar de ser.
"""

from collections.abc import Iterable

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# Métodos que não alteram estado. Não precisam da checagem — e o preflight do CORS
# (OPTIONS) não pode ser barrado aqui, senão a própria tela deixaria de funcionar.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class OriginCheckMiddleware:
    """Recusa com 403 a escrita cujo `Origin` não está em `allowed_origins`.

    Requisição **sem** `Origin` passa: é o Server Component do Next, o `curl`, o
    healthcheck — nada disso é um navegador sendo usado contra o titular. O
    navegador sempre manda `Origin` em POST, PATCH e DELETE vindos de outra origem;
    `Origin: null` (iframe com sandbox, arquivo local) também é recusado.
    """

    def __init__(self, app: ASGIApp, allowed_origins: Iterable[str]) -> None:
        self.app = app
        self.allowed = frozenset(origin.rstrip("/") for origin in allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            origin = Headers(scope=scope).get("origin")
            if origin is not None and origin.rstrip("/") not in self.allowed:
                response = PlainTextResponse("origem não autorizada", status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
