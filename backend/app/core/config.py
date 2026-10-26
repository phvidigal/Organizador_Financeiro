"""Configuração da aplicação, carregada do ambiente uma única vez no boot."""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Nomes pelos quais o app é alcançado sem sair da máquina: o navegador do host e a
# rede interna do compose. Fora daqui, só com autenticação (ver `_hosts_are_local`).
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "backend"})


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

    # Valores aceitos no header `Host`. É o que barra DNS rebinding: uma página
    # maliciosa que faça o próprio domínio resolver para 127.0.0.1 chega aqui com
    # `Host: dominio-dela`, e é recusada antes de qualquer rota. `backend` é o nome
    # do serviço no compose — o Server Component do Next chama por ele.
    #
    # Só nomes locais, e isso é verificado no boot (ver `_hosts_are_local`).
    allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: sorted(LOCAL_HOSTS)
    )

    # `GET /pluggy/diagnostics` despeja a resposta da Pluggy. Desligado por padrão:
    # liga-se para depurar um connector novo e desliga-se em seguida.
    enable_pluggy_diagnostics: bool = False

    # `NoDecode` desliga o parse automático que o pydantic-settings faz em campos
    # de tipo complexo: sem ele, a fonte de ambiente tenta `json.loads` na string
    # e falha antes de o validador abaixo receber o valor.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    @field_validator("cors_origins", "allowed_hosts", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        # No .env a variável é uma string separada por vírgula.
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    @field_validator("allowed_hosts")
    @classmethod
    def _hosts_are_local(cls, v: list[str]) -> list[str]:
        """Recusa subir aceitando host de fora da máquina.

        **Não há autenticação**: quem alcança a API lê o extrato inteiro. Abrir o app
        para o celular ou outro computador exige pôr o IP ou o nome da máquina aqui
        — e é exatamente esse passo que precisa esbarrar num erro dizendo o que falta,
        e não funcionar em silêncio. Quando houver login, esta trava sai junto.
        """
        foreign = [host for host in v if host not in LOCAL_HOSTS]
        if foreign:
            raise ValueError(
                f"ALLOWED_HOSTS com host fora da máquina ({', '.join(foreign)}): acesso "
                "pela rede exige autenticação, que ainda não existe"
            )
        return v

    @field_validator(
        "pluggy_client_id",
        "pluggy_client_secret",
        "pluggy_item_id",
        "app_encryption_key",
        mode="before",
    )
    @classmethod
    def _strip_optional_secret(cls, v: object) -> object:
        """Remove espaço em volta de valor colado no `.env`, e trata vazio como ausente.

        `PLUGGY_CLIENT_ID= abc` (com o espaço depois do `=`) é artefato comum de
        copiar do navegador, e o espaço viaja junto até o corpo da requisição. A
        Pluggy responde 401 sem dizer por quê, e o valor *parece* certo em qualquer
        inspeção visual — o erro só aparece contando caracteres.

        Vazio vira `None` para que "não configurado" tenha um significado só: é o
        que faz `get_pluggy_client` responder 503 explicando o que falta, em vez de
        tentar autenticar com string vazia e devolver um 401 confuso.
        """
        if isinstance(v, str):
            return v.strip() or None
        return v

    @field_validator("pluggy_base_url", "ollama_base_url", "ollama_model", mode="before")
    @classmethod
    def _strip_url(cls, v: object) -> object:
        """Mesma limpeza, sem converter vazio em `None`: estes campos são `str`."""
        return v.strip() if isinstance(v, str) else v

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
