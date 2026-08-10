"""Configuração da aplicação, carregada do ambiente uma única vez no boot."""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Fora do Docker, lê o .env da raiz do repositório. Dentro do Docker, as
        # variáveis já vêm do compose e o arquivo simplesmente não existe.
        env_file="../.env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    # --- Banco ---------------------------------------------------------------
    # Conexão da aplicação: role app_user, sujeito ao RLS.
    database_url: PostgresDsn
    # Conexão das migrations: role owner, que ignora RLS e pode criar tabelas.
    alembic_database_url: PostgresDsn | None = None

    test_database_url: PostgresDsn | None = None
    test_app_database_url: PostgresDsn | None = None

    # --- Pluggy (Fase 2, ainda sem uso em código) ----------------------------
    pluggy_client_id: str | None = None
    pluggy_client_secret: str | None = None
    pluggy_base_url: str = "https://api.pluggy.ai"
    pluggy_item_id: str | None = None

    # --- Ollama (Fase 3) -----------------------------------------------------
    ollama_base_url: str = "http://host.docker.internal:11434"
    ollama_model: str = "qwen3.5:9b"

    # --- Segurança / LGPD ----------------------------------------------------
    app_encryption_key: str | None = None

    # `NoDecode` desliga o parse automático que o pydantic-settings faz em campos
    # de tipo complexo: sem ele, a fonte de ambiente tenta `json.loads` na string
    # e falha antes de o validador abaixo receber o valor.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        # No .env a variável é uma string separada por vírgula.
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    @property
    def sync_database_url(self) -> str:
        """DSN síncrona para o Alembic.

        Prefere `ALEMBIC_DATABASE_URL` (role owner). Sem ela, cai na DSN da
        aplicação com o driver trocado — o que funciona quando app e migrations
        usam o mesmo role, mas falha ao criar tabelas se `app_user` estiver
        corretamente sem privilégio de DDL.
        """
        url = str(self.alembic_database_url or self.database_url)
        return url.replace("+asyncpg", "+psycopg")


@lru_cache
def get_settings() -> Settings:
    """Cacheado: a validação acontece no primeiro acesso (boot), não a cada request.

    Faltando variável obrigatória, o processo morre ao subir em vez de devolver
    500 no primeiro request.
    """
    return Settings()  # type: ignore[call-arg]
