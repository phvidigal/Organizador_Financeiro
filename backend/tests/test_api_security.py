"""As defesas de borda de `app/core/security.py`, contra o app montado de verdade.

Testa o `create_app()` inteiro, e não os middlewares isolados: o que importa é a
ordem em que eles envolvem as rotas, e um teste de unidade passaria verde com o
`TrustedHostMiddleware` registrado por dentro do CORS.

`POST /categorization/reset` é o alvo dos casos de escrita porque é o exemplo
real do ataque: uma requisição simples, sem corpo, que um formulário de outro site
consegue disparar sem preflight.
"""

import httpx
import pytest

from app.core.tenancy import get_tenant_session, resolve_tenant_id
from app.main import create_app
from tests.conftest import TENANT_A

# Mesmo event loop das fixtures de sessão (ver pyproject.toml).
pytestmark = pytest.mark.asyncio(loop_scope="session")

EVIL = "https://evil.example"


@pytest.fixture
def api(app_tenant_session, tenants):
    def build(host: str = "localhost"):
        app = create_app()

        async def override_session():
            async with app_tenant_session(TENANT_A) as session:
                yield session

        app.dependency_overrides[get_tenant_session] = override_session
        app.dependency_overrides[resolve_tenant_id] = lambda: TENANT_A
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=f"http://{host}:8000"
        )

    return build


async def test_foreign_host_is_refused_before_any_route(api) -> None:
    """DNS rebinding: o navegador acha que fala com `evil.example`, e o `Host` diz isso."""
    async with api("evil.example") as client:
        response = await client.get("/transactions")

    assert response.status_code == 400


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "backend"])
async def test_local_hosts_are_accepted(api, host) -> None:
    """`backend` é como o Server Component do Next chama, de dentro do compose."""
    async with api(host) as client:
        response = await client.get("/transactions")

    assert response.status_code == 200


async def test_write_from_foreign_origin_is_refused(api) -> None:
    async with api() as client:
        response = await client.post("/categorization/reset", headers={"Origin": EVIL})

    assert response.status_code == 403


async def test_write_from_null_origin_is_refused(api) -> None:
    """`Origin: null` é o que um iframe com sandbox manda — e não é a tela do app."""
    async with api() as client:
        response = await client.post("/categorization/reset", headers={"Origin": "null"})

    assert response.status_code == 403


async def test_write_from_the_frontend_origin_passes(api) -> None:
    async with api() as client:
        response = await client.post(
            "/categorization/reset", headers={"Origin": "http://localhost:3000"}
        )

    assert response.status_code == 200


async def test_write_without_origin_passes(api) -> None:
    """Server Component, curl e healthcheck não mandam `Origin`, e não são navegador."""
    async with api() as client:
        response = await client.post("/categorization/reset")

    assert response.status_code == 200


async def test_read_from_foreign_origin_is_left_to_cors(api) -> None:
    """Leitura não é barrada aqui: o CORS já esconde a resposta de outra origem, e o
    rebinding — o caso em que ele não esconde — é barrado pelo `Host`."""
    async with api() as client:
        response = await client.get("/transactions", headers={"Origin": EVIL})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
