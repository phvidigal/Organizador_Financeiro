"""Prompt, JSON Schema e leitura da resposta. Puro — nem httpx, nem SQLAlchemy.

Fica isolado porque é a parte que mais vai mudar e a que mais precisa de teste:
ajustar o prompt é iteração, e iteração sem teste vira regressão silenciosa numa
categorização que ninguém confere.

**O `enum` é o coração da coisa.** O Ollama aceita um JSON Schema completo no
parâmetro `format` e restringe a gramática da geração a ele — colocando a lista de
categorias válidas dentro do schema, uma resposta fora da taxonomia deixa de ser
possível por construção. A alternativa (string livre + resolução depois) gastaria
uma chamada de GPU para produzir algo que já nasce inválido.
"""

import json
import re
import uuid
from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.categorization.catalog import CategoryCatalog
from app.services.categorization.errors import OllamaResponseError

# Alguns modelos de raciocínio (o qwen3 é um) escrevem o raciocínio no conteúdo
# quando `think: false` não é honrado. O `format` restringe a gramática da resposta,
# mas o bloco de pensamento sai antes dela — e aí o `json.loads` falha num corpo
# que, tirando o prefixo, estava perfeito.
_THINK_BLOCK = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)

_ERROR_EXCERPT = 300

SYSTEM_PROMPT = """\
Você classifica transações financeiras de uma pessoa física brasileira.

Receberá uma lista fechada de categorias e uma transação. Escolha exatamente uma \
categoria da lista e informe sua confiança entre 0 e 1.

Regras:
1. Use a descrição do lançamento como evidência principal. Nomes de \
estabelecimento, bandeiras e siglas comuns no extrato brasileiro (por exemplo \
"PIX", "TED", "IFD*IFOOD", "PAG*", "MP*") são pistas.
2. O "palpite do agregador", quando presente, é apenas uma pista. Ele costuma usar \
categorias genéricas como balde e erra com frequência em compras no débito; \
discorde dele quando a descrição indicar outra coisa.
3. "Transferências" é para dinheiro que continua com o titular: movimentação \
entre contas dele mesmo e pagamento de fatura de cartão. NÃO é um balde para todo \
Pix.
3a. Pix RECEBIDO de outra pessoa ou de empresa é receita. Só é \
"Transferência entre contas próprias" quando houver evidência de que o remetente é \
o próprio titular — tipicamente o mesmo nome. Valor redondo que se repete todo mês, \
ou remetente que é pessoa jurídica, é sinal de renda.
3b. Pix ENVIADO em pagamento de algo (restaurante, aluguel, serviço, pessoa que \
prestou serviço) é despesa, e deve receber a categoria do que foi pago, não \
"Pix enviado". Use "Pix enviado" apenas quando o destino for conta do próprio \
titular ou quando não houver como saber o motivo.
3c. **Quando a descrição não permitir decidir se um Pix é transferência ou não, \
use confiança BAIXA (abaixo de 0.5).** Só o titular sabe quem é o remetente ou o \
destinatário; um palpite com confiança alta impede que ele seja consultado.
4. Aplicar ou resgatar investimento (CDB, RDB, Tesouro, ações, fundos, cripto, \
previdência) pertence à árvore "Investimentos": o principal continua sendo do \
titular, só mudou de conta. O RENDIMENTO é outra coisa — juros, dividendos e \
"valor recebido de investimentos" são dinheiro novo, e vão para \
"Receitas > Rendimentos e investimentos". A pergunta que separa as duas é se o \
principal está se movendo ou se está sendo remunerado.
5. Prefira a subcategoria mais específica que a descrição sustente. Se a descrição \
não permitir distinguir entre subcategorias, escolha a categoria raiz.
6. Não invente categoria: só existe o que está na lista.
7. Confiança alta significa que a descrição é conclusiva. Descrição genérica \
("COMPRA NO DEBITO", "PAGAMENTO"), sem nome de estabelecimento, é confiança baixa \
— é melhor que a transação vá para revisão do que receber uma categoria inventada. \
A confiança é o seu único jeito de fazer uma pergunta: baixa significa "não tenho \
como saber isto sozinho".

Responda apenas com o objeto JSON pedido.\
"""


@dataclass(frozen=True)
class TransactionForPrompt:
    """O que o modelo vê de uma transação.

    `description_raw` e não `description_clean`: a descrição crua é o insumo do LLM
    por decisão da Fase 1, e limpar antes destruiria justamente os prefixos que
    identificam o meio de pagamento.
    """

    id: uuid.UUID
    date: date_type
    amount: Decimal
    description_raw: str
    currency_code: str = "BRL"
    merchant: dict[str, Any] | None = None
    pluggy_category_name: str | None = None
    pluggy_category_id: str | None = None
    account_type: str | None = None


@dataclass(frozen=True)
class Answer:
    """A resposta do modelo, já validada quanto à *forma* (não quanto ao conteúdo)."""

    category: str
    confidence: Decimal | None


def build_schema(labels: list[str]) -> dict[str, Any]:
    """JSON Schema da resposta, com as categorias do tenant no `enum`.

    Gerado a cada execução, e não constante de módulo: categoria criada ou
    desativada pelo usuário entra e sai sozinha na rodada seguinte.
    """
    return {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": labels},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["category", "confidence"],
        "additionalProperties": False,
    }


def format_amount(amount: Decimal, currency_code: str = "BRL") -> str:
    """Valor em notação pt-BR, com o sinal preservado.

    O sinal é informação, não formatação: a convenção do banco é "negativo = saída",
    e é o que separa uma compra de um estorno na mesma descrição.
    """
    quantized = Decimal(amount).quantize(Decimal("0.01"))
    signal = "-" if quantized < 0 else ""
    integer, _, decimals = f"{abs(quantized):.2f}".partition(".")
    grouped = f"{int(integer):,}".replace(",", ".")
    return f"{signal}{grouped},{decimals} {currency_code}"


def _merchant_name(merchant: dict[str, Any] | None) -> str | None:
    if not isinstance(merchant, dict):
        return None
    for key in ("name", "businessName", "legalBusinessName"):
        value = merchant.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _account_label(account_type: str | None) -> str:
    """Contexto que muda a leitura da descrição.

    Num cartão de crédito, "PAGAMENTO" quase sempre é a fatura sendo quitada
    (TRANSFER); numa conta corrente, é qualquer coisa.
    """
    return {"CREDIT": "cartão de crédito", "BANK": "conta bancária"}.get(
        account_type or "", "desconhecida"
    )


def build_messages(
    tx: TransactionForPrompt, catalog: CategoryCatalog
) -> list[dict[str, str]]:
    """Mensagens de `POST /api/chat`, uma transação por chamada."""
    lines = [
        "Categorias disponíveis:",
        *(f"- {label}" for label in catalog.labels),
        "",
        "Transação:",
        f"  data: {tx.date.isoformat()}",
        f"  valor: {format_amount(tx.amount, tx.currency_code)}  (negativo = saída)",
        f"  conta: {_account_label(tx.account_type)}",
        f"  descrição: {tx.description_raw}",
    ]

    merchant = _merchant_name(tx.merchant)
    if merchant:
        lines.append(f"  estabelecimento: {merchant}")
    if tx.pluggy_category_name:
        lines.append(f"  palpite do agregador: {tx.pluggy_category_name}")

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def strip_think_block(content: str) -> str:
    """Remove um `<think>…</think>` no começo do conteúdo."""
    return _THINK_BLOCK.sub("", content, count=1)


def parse_response(content: str) -> Answer:
    """Lê o JSON da resposta. Levanta `OllamaResponseError` no que não der para usar.

    `parse_float=Decimal` porque a confiança vai para uma coluna `NUMERIC(4,3)`:
    `Decimal(0.85)` não é 0.85, e o erro entra na conversão de `float`, não na de
    string.

    Confiança ausente ou fora da faixa vira `None` em vez de erro. O `enum` do
    schema garante a categoria, que é o que importa; perder a transação inteira por
    causa de um número que já é reconhecidamente mal calibrado seria trocar o dado
    bom pelo ruim.
    """
    try:
        body = json.loads(strip_think_block(content), parse_float=Decimal)
    except ValueError as exc:
        raise OllamaResponseError(
            f"resposta do modelo não é JSON: {content[:_ERROR_EXCERPT]!r}"
        ) from exc

    if not isinstance(body, dict):
        raise OllamaResponseError(
            f"resposta do modelo não é um objeto: {content[:_ERROR_EXCERPT]!r}"
        )

    category = body.get("category")
    if not isinstance(category, str) or not category.strip():
        raise OllamaResponseError(
            f"resposta do modelo sem campo 'category': {content[:_ERROR_EXCERPT]!r}"
        )

    return Answer(category=category.strip(), confidence=_confidence(body.get("confidence")))


def _confidence(value: object) -> Decimal | None:
    """Normaliza a confiança para caber em `NUMERIC(4,3)` com o CHECK de 0 a 1."""
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, int | float | Decimal | str):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 0 or number > 1:
        # Fora da faixa é resposta que ignorou o schema. Descartar o número e manter
        # a categoria é melhor que gravar algo que o CHECK `confidence_range`
        # rejeitaria no meio do lote.
        return None
    return number.quantize(Decimal("0.001"))
