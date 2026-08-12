"""Higienização das variáveis de ambiente.

Módulo síncrono, sem banco: `Settings` é validação pura do que veio do ambiente.

As variáveis entram por `monkeypatch.setenv`, e não por kwargs do construtor, para
exercitar o caminho real — o pydantic-settings só faz o parse da fonte de ambiente
por lá; kwargs em maiúsculo cairiam no `extra="ignore"` e o teste passaria verde
sem testar nada.
"""

import pytest

from app.core.config import Settings

# Único campo obrigatório; sem ele a validação falha antes de chegar ao que importa.
DATABASE_URL = "postgresql+asyncpg://u:p@db:5432/finance"

PLUGGY_VARS = (
    "PLUGGY_CLIENT_ID",
    "PLUGGY_CLIENT_SECRET",
    "PLUGGY_ITEM_ID",
    "PLUGGY_BASE_URL",
    "OLLAMA_MODEL",
    "CORS_ORIGINS",
)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    """Ambiente limpo. `_env_file=None` impede que o `.env` real vaze para o teste."""
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    for name in PLUGGY_VARS:
        monkeypatch.delenv(name, raising=False)

    def build(**values: str) -> Settings:
        for name, value in values.items():
            monkeypatch.setenv(name, value)
        return Settings(_env_file=None)  # type: ignore[call-arg]

    return build


def test_pasted_credentials_are_stripped(env) -> None:
    """`PLUGGY_CLIENT_ID= abc` é artefato de copiar do navegador. O espaço viajaria
    até o corpo da requisição, a Pluggy responderia 401, e o valor pareceria certo
    em qualquer inspeção visual — o erro só aparece contando caracteres."""
    settings = env(
        PLUGGY_CLIENT_ID=" 0a1b2c3d-4e5f-6789-abcd-ef0123456789",
        PLUGGY_CLIENT_SECRET="segredo-com-espaco-no-fim  ",
        PLUGGY_ITEM_ID="\t46a4a03c-0000-0000-0000-000000000000\n",
    )

    assert settings.pluggy_client_id == "0a1b2c3d-4e5f-6789-abcd-ef0123456789"
    assert settings.pluggy_client_secret == "segredo-com-espaco-no-fim"
    assert settings.pluggy_item_id == "46a4a03c-0000-0000-0000-000000000000"


def test_blank_optional_setting_means_absent(env) -> None:
    """Um significado só para "não configurado".

    É o que faz `get_pluggy_client` responder 503 dizendo o que falta, em vez de
    tentar autenticar com string vazia e devolver um 401 confuso.
    """
    settings = env(PLUGGY_CLIENT_ID="   ", PLUGGY_CLIENT_SECRET="")

    assert settings.pluggy_client_id is None
    assert settings.pluggy_client_secret is None


def test_urls_are_stripped_without_becoming_none(env) -> None:
    """Estes campos são `str` com default; virar `None` quebraria a validação."""
    settings = env(PLUGGY_BASE_URL=" https://api.pluggy.ai ", OLLAMA_MODEL=" qwen3.5:9b ")

    assert settings.pluggy_base_url == "https://api.pluggy.ai"
    assert settings.ollama_model == "qwen3.5:9b"


def test_defaults_survive_when_nothing_is_set(env) -> None:
    settings = env()

    assert settings.pluggy_base_url == "https://api.pluggy.ai"
    assert settings.pluggy_client_id is None
    assert settings.environment == "development"


@pytest.mark.parametrize("raw", ["http://a:3000,http://b:3001", " http://a:3000 , http://b:3001 "])
def test_cors_origins_split_ignores_surrounding_space(env, raw: str) -> None:
    assert env(CORS_ORIGINS=raw).cors_origins == ["http://a:3000", "http://b:3001"]
