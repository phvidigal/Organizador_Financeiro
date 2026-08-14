"""Schemas do resumo do dashboard.

Duas decisões atravessam este arquivo inteiro.

**`TRANSFER` tem bloco próprio e fica fora de `net`.** É a razão de o campo `kind`
existir: aplicar num CDB, pagar a fatura do cartão ou mandar um Pix para a própria
conta move dinheiro sem ser gasto nem receita. Somá-los inflaria as duas pontas e
contaria o mesmo dinheiro duas vezes.

**Cada `kind` carrega a fatia que ainda está sob revisão.** Sem isso, quem lê o
total não tem como saber quanto dele é palpite do LLM esperando resposta — e no
estado atual isso não é detalhe: 99% da receita está em `NEEDS_REVIEW`, porque a
regra 3c do `SYSTEM_PROMPT` manda o modelo baixar a confiança quando só o titular
sabe quem mandou o Pix. Um número sem essa companhia seria um total errado com cara
de certo.
"""

import uuid
from datetime import date as date_type

from pydantic import BaseModel

from app.schemas.categorization import QueueCountsRead
from app.schemas.common import Money


class KindTotal(BaseModel):
    """Soma de um `kind` no período, com a parte que ainda não foi respondida."""

    # Sinal preservado: despesa sai negativa, como está no banco. Devolver
    # magnitude positiva aqui contradiria "negativo = saída, sempre" na única
    # fronteira em que a convenção ainda vale — a tela é quem formata.
    total: Money
    count: int

    # `NEEDS_REVIEW` significa "o modelo perguntou", não "está errado". Estas duas
    # colunas medem o tamanho da pergunta, não o do erro.
    needs_review_total: Money
    needs_review_count: int


class CategoryTotal(BaseModel):
    """Uma linha da quebra por categoria."""

    # NULL quando a transação não tem categoria — vira um balde explícito em vez de
    # sumir da soma.
    category_id: uuid.UUID | None
    # Rótulo qualificado ("Alimentação > Delivery"), resolvido pelo mesmo
    # `load_catalog` que alimenta `GET /categories` e o `enum` do Ollama.
    label: str
    kind: str
    total: Money
    count: int
    needs_review_count: int


class MonthTotal(BaseModel):
    """Um mês da série temporal. `month` é `YYYY-MM`, que ordena sozinho."""

    month: str
    income: Money
    expense: Money
    transfer: Money


class DashboardSummary(BaseModel):
    """O resumo inteiro numa chamada.

    `date_from`/`date_to` em vez de um objeto `period` com campo `from`: `from` é
    palavra reservada em Python e exigiria um alias, que vazaria para todo lugar que
    tocasse o schema.
    """

    date_from: date_type
    date_to: date_type

    income: KindTotal
    expense: KindTotal
    transfer: KindTotal

    # `income + expense`, com `transfer` deliberadamente fora.
    net: Money

    by_category: list[CategoryTotal]
    by_month: list[MonthTotal]

    # Contagens do mesmo recorte de período, e não da base inteira — tudo nesta
    # resposta é do período filtrado, e misturar os dois faria a faixa de aviso
    # discordar dos números logo abaixo dela.
    queue: QueueCountsRead
