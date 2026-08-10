"""Estados do domínio.

Todos viram `VARCHAR + CHECK` no Postgres (`native_enum=False`), nunca `CREATE TYPE`.
Motivo: tipo ENUM nativo não permite remover valor, e adicionar exige `ALTER TYPE`
numa migration dedicada. `CategorySource` já vai crescer — `EMBEDDING` só entra
com a pipeline híbrida — e um CHECK é trivial de reescrever.
"""

from enum import StrEnum


class TransactionSource(StrEnum):
    """Origem do dado. Define qual constraint de unicidade se aplica à linha."""

    PLUGGY = "PLUGGY"
    OFX = "OFX"
    CSV = "CSV"
    MANUAL = "MANUAL"


class TransactionKind(StrEnum):
    """Natureza da transação — eixo separado da categoria.

    Existe porque, no Brasil, Pix e pagamento de fatura dominam o extrato e não
    são gasto nem receita: são dinheiro mudando de lugar. Sem este campo, um Pix
    de R$ 2.000 entre contas do próprio titular apareceria como R$ 2.000 de
    despesa numa conta e R$ 2.000 de receita na outra, inflando as duas pontas do
    dashboard.

    Também é o que torna seguro contar cada compra do cartão como despesa na data
    da compra: o pagamento da fatura entra como TRANSFER e não soma de novo.

    Categoria responde "no que foi gasto"; `kind` responde "isso é gasto?". Uma
    categoria não substitui a outra — o dashboard filtra por `kind` e agrupa por
    categoria.
    """

    INCOME = "INCOME"
    EXPENSE = "EXPENSE"
    TRANSFER = "TRANSFER"


class CategorizationStatus(StrEnum):
    """Responde "já processei esta transação?" — é a fila do job da Fase 3."""

    PENDING = "PENDING"
    CATEGORIZED = "CATEGORIZED"
    NEEDS_REVIEW = "NEEDS_REVIEW"  # categorizada, mas com confiança baixa
    FAILED = "FAILED"


class CategorySource(StrEnum):
    """Responde "quem decidiu a categoria?".

    Eixo separado de `CategorizationStatus` de propósito: é o que permite medir
    se a categorização nativa da Pluggy basta (Fase 2) e comparar o acerto do LLM
    contra a correção manual (Fase 4). Com um campo só, essa medição exigiria
    migração de dados depois.

    `MANUAL` tem peso especial: o upsert do sync nunca sobrescreve a categoria de
    uma linha marcada assim.
    """

    PLUGGY = "PLUGGY"
    RULE = "RULE"
    EMBEDDING = "EMBEDDING"  # reservado para a pipeline híbrida
    LLM = "LLM"
    MANUAL = "MANUAL"


class AccountType(StrEnum):
    """Espelha o campo `type` da API Pluggy."""

    BANK = "BANK"
    CREDIT = "CREDIT"


class AccountSubtype(StrEnum):
    """Espelha o campo `subtype` da API Pluggy."""

    CHECKING_ACCOUNT = "CHECKING_ACCOUNT"
    SAVINGS_ACCOUNT = "SAVINGS_ACCOUNT"
    CREDIT_CARD = "CREDIT_CARD"


class ConnectionStatus(StrEnum):
    """Campo `status` do Item da Pluggy."""

    UPDATING = "UPDATING"
    UPDATED = "UPDATED"
    WAITING_USER_INPUT = "WAITING_USER_INPUT"
    LOGIN_ERROR = "LOGIN_ERROR"
    OUTDATED = "OUTDATED"
    ERROR = "ERROR"


class MatchType(StrEnum):
    """Tipos de regra previstos para a pipeline de regras (tabela vazia por ora)."""

    EXACT = "EXACT"
    CONTAINS = "CONTAINS"
    REGEX = "REGEX"
    AMOUNT_RANGE = "AMOUNT_RANGE"
