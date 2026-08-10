"""Verificação de boot do isolamento.

Este é um teste sobre o teste: garante que a rede de segurança realmente pega o
caso que ela existe para pegar. Um guard que aceita tudo é pior que nenhum guard,
porque dá a impressão de proteção.
"""

import pytest

from app.core.rls_guard import RLSNotEnforcedError, assert_rls_enforced

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_app_role_passes(app_engine) -> None:
    """A conexão real da aplicação (`app_user`) está sujeita às policies."""
    await assert_rls_enforced(app_engine)


async def test_owner_role_is_rejected(admin_engine) -> None:
    """O cenário que motivou o guard: alguém troca `DATABASE_URL` para o owner
    durante uma depuração e esquece de voltar. O app funcionaria perfeitamente e
    enxergaria todos os tenants."""
    with pytest.raises(RLSNotEnforcedError) as exc:
        await assert_rls_enforced(admin_engine)

    # A mensagem precisa dizer o que fazer, não só que algo está errado — quem a
    # lê está olhando um container que não sobe.
    assert "APP_DB_USER" in str(exc.value)
