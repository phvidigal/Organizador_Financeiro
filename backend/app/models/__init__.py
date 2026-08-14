"""Modelos do domínio.

Todos importados aqui para que `Base.metadata` esteja completo quando o Alembic
carregar este pacote. Um modelo ausente desta lista some do autogenerate e a
migration correspondente nunca é gerada.
"""

from app.models.account import Account
from app.models.bank_connection import BankConnection
from app.models.base import Base
from app.models.categorization_review import CategorizationReview
from app.models.category import Category
from app.models.category_rule import CategoryRule
from app.models.enums import (
    AccountSubtype,
    AccountType,
    CategorizationStatus,
    CategorySource,
    ConnectionStatus,
    MatchType,
    TransactionKind,
    TransactionSource,
)
from app.models.tenant import Tenant, User
from app.models.transaction import Transaction

# Tabelas com `tenant_id`, portanto sujeitas a Row-Level Security.
# A migration inicial e o teste de isolamento leem esta lista, para que criar uma
# tabela nova sem policy apareça como teste vermelho e não como vazamento silencioso.
TENANT_SCOPED_TABLES: tuple[str, ...] = (
    "users",
    "bank_connections",
    "accounts",
    "categories",
    "transactions",
    "category_rules",
    "categorization_reviews",
)

__all__ = [
    "Account",
    "AccountSubtype",
    "AccountType",
    "BankConnection",
    "Base",
    "CategorizationReview",
    "CategorizationStatus",
    "Category",
    "CategoryRule",
    "CategorySource",
    "ConnectionStatus",
    "MatchType",
    "TENANT_SCOPED_TABLES",
    "Tenant",
    "Transaction",
    "TransactionKind",
    "TransactionSource",
    "User",
]
