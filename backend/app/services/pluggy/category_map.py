"""De/para entre a taxonomia da Pluggy e a nossa.

Existe porque as duas taxonomias não se parecem: a nossa é artesanal e em pt-BR
(`Alimentação`, `Serviços financeiros`), escrita para o dashboard da Fase 4; a da
Pluggy é a dela. Casar por nome acertaria quase nada.

Preencher `categories.pluggy_category_id` é o que permite à Fase 3 comparar o
palpite da Pluggy com a categoria escolhida pelo LLM — divergência entre duas
fontes independentes é sinal de confiança melhor que a autoavaliação de uma só.

**Isto não bloqueia o sync.** `transactions.pluggy_category_id` e
`pluggy_category_name` vêm direto do payload da transação, sem passar por aqui.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category

logger = logging.getLogger(__name__)

# Chaveado pelo `description` em inglês da Pluggy, não pelo id numérico nem pela
# tradução: o id é estável mas ilegível numa revisão de código, e
# `descriptionTranslated` muda sem aviso. O inglês é o meio-termo.
#
# 🔬 Escrito a partir da resposta real de `GET /categories` (130 categorias, duas
# de profundidade). Só entram pares de alta confiança: uma categoria nossa sem
# mapeamento é um dado faltando, que o relatório expõe; uma mapeada errado é um
# dado *falso*, que ninguém notaria até conferir uma categorização na tela.
#
# **A relação é 1:1 por limitação do schema.** `categories.pluggy_category_id` é
# uma coluna só, então uma categoria nossa não pode apontar para duas da Pluggy.
# Onde a taxonomia deles é mais fina que a nossa — "Online Courses" / "University"
# / "School" contra o nosso "Cursos e mensalidades" — a entrada fica de fora, e a
# Fase 3 vê essa transação como "sem contraparte" em vez de "discordância". É o
# erro menos ruim dos dois: subestimar concordância não inventa acerto.
PLUGGY_TO_LOCAL: dict[str, str] = {
    # --- Receitas ---
    "Income": "Receitas",
    "Salary": "Salário",
    "Entrepreneurial activities": "Freelance / PJ",
    "Proceeds interests and dividends": "Rendimentos e investimentos",
    "Non-recurring income": "Outras receitas",
    # --- Alimentação ---
    "Food and drinks": "Alimentação",
    "Eating out": "Restaurantes e bares",
    "Food delivery": "Delivery",
    "Groceries": "Supermercado",
    # --- Compras ---
    "Shopping": "Compras",
    "Electronics": "Eletrônicos",
    "Clothing": "Vestuário",
    "Houseware": "Casa e decoração",
    "Bookstore": "Livros e materiais",
    # --- Educação ---
    "Education": "Educação",
    # --- Lazer ---
    "Leisure": "Lazer",
    "Cinema, theater and concerts": "Eventos e cultura",
    "Digital services": "Streaming e assinaturas",
    "Travel": "Viagens",
    # --- Moradia ---
    "Housing": "Moradia",
    "Rent": "Aluguel / Financiamento",
    "Utilities": "Água, luz e gás",
    "Telecommunications": "Internet e telefone",
    # --- Saúde ---
    "Healthcare": "Saúde",
    "Pharmacy": "Farmácia",
    "Hospital clinics and labs": "Consultas e exames",
    "Gyms and fitness centers": "Academia e bem-estar",
    "Health insurance": "Plano de saúde",
    # --- Serviços financeiros ---
    "Bank fees": "Tarifas bancárias",
    "Taxes": "Impostos",
    "Interests charged": "Juros e encargos",
    "Insurance": "Seguros",
    # --- Investimentos ---
    # 🔬 "Investments" e "Fixed income" apareceram no extrato real. A Pluggy trata
    # investimento como *gasto*; nós tratamos como TRANSFER (ver a migration
    # 0003). O de/para liga as duas taxonomias mesmo assim — ele existe para
    # comparar palpites, não para importar a semântica deles.
    #
    # Ficam de fora, e cada uma por um motivo:
    # - "Automatic investment" e "Taxes on investments" disputariam "Investimentos"
    #   e "Impostos" com pares melhores. Duas da Pluggy para uma nossa é conflito,
    #   e o de/para recusa os dois lados em vez de deixar a ordem do dicionário
    #   decidir;
    # - "Pension" e "Retirement" disputariam "Previdência privada", e qual das
    #   duas significa PGBL/VGBL contra aposentadoria do INSS não dá para afirmar
    #   sem medir. Mapeamento errado é pior que mapeamento faltando: o faltando
    #   aparece no relatório, o errado vira "concordância" inventada;
    # - "Criptoativos" não tem contraparte na taxonomia deles.
    "Investments": "Investimentos",
    "Fixed income": "Renda fixa",
    "Variable income": "Renda variável",
    "Mutual funds": "Fundos de investimento",
    # --- Transferências ---
    # "Transfer - PIX" fica de fora: a Pluggy não distingue enviado de recebido, e
    # nós distinguimos. A direção está no sinal do valor, não na categoria.
    "Transfers": "Transferências",
    "Credit card payment": "Pagamento de cartão",
    "Same person transfer": "Transferência entre contas próprias",
    # --- Transporte ---
    "Transportation": "Transporte",
    "Gas stations": "Combustível",
    "Vehicle maintenance": "Manutenção do veículo",
    "Taxi and ride-hailing": "Transporte por aplicativo",
    "Public transportation": "Transporte público",
    # --- Outros ---
    "Other": "Não categorizado",
}


@dataclass
class CategoryMapReport:
    """Resultado de uma passada do de/para.

    Enquanto `PLUGGY_TO_LOCAL` estiver vazio, este relatório é o dump que permite
    escrevê-lo: `unmatched_pluggy` lista o que a Pluggy tem e nós ignoramos,
    `unmapped_local` lista as nossas categorias que continuam sem contraparte.
    """

    mapped: int = 0
    already_mapped: int = 0
    unmatched_pluggy: list[str] = field(default_factory=list)
    unmapped_local: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


async def sync_category_map(
    session: AsyncSession,
    *,
    pluggy_categories: list[dict[str, Any]],
) -> CategoryMapReport:
    """Preenche `categories.pluggy_category_id` a partir de `GET /categories`.

    Idempotente: reexecutar não muda nada. O tenant vem do escopo da sessão (RLS),
    não de um parâmetro — é o que garante que ninguém escreva na taxonomia alheia
    por engano.

    Um mapeamento já gravado **não é sobrescrito** com valor diferente; a
    divergência vai para `conflicts`. Ela significa que a Pluggy renomeou ou
    renumerou uma categoria, e resolver isso em silêncio é o tipo de coisa que a
    Fase 3 descobriria como acerto medido errado.
    """
    report = CategoryMapReport()

    by_description = {
        str(c["description"]): str(c["id"])
        for c in pluggy_categories
        if c.get("description") and c.get("id")
    }

    result = await session.execute(
        select(Category.id, Category.name, Category.pluggy_category_id)
    )
    local_rows = list(result)

    # O índice único de categorias é `(tenant_id, parent_id, name)`, então o mesmo
    # nome pode existir sob pais diferentes. Nome ambíguo vira conflito em vez de
    # um chute sobre qual dos dois o de/para queria dizer.
    local_by_name: dict[str, list[Any]] = defaultdict(list)
    for row in local_rows:
        local_by_name[row.name].append(row)

    to_update: dict[str, str] = {}
    # Ids locais já reivindicados. Separado de `to_update` porque uma disputa
    # remove a entrada de lá, e sem esta marca um terceiro pretendente entraria.
    claimed: set[str] = set()

    for pluggy_description, local_name in PLUGGY_TO_LOCAL.items():
        pluggy_id = by_description.get(pluggy_description)
        if pluggy_id is None:
            report.conflicts.append(
                f"'{pluggy_description}' está no de/para mas não veio de GET /categories"
            )
            continue

        candidates = local_by_name.get(local_name, [])
        if not candidates:
            # A taxonomia do tenant não tem essa categoria. Pode ser um de/para
            # desatualizado, ou simplesmente um tenant que ainda não foi semeado.
            report.conflicts.append(
                f"'{local_name}' não existe na taxonomia deste tenant; "
                f"'{pluggy_description}' não foi mapeada"
            )
            continue
        if len(candidates) > 1:
            # O índice único é `(tenant_id, parent_id, name)`, então o mesmo nome
            # pode existir sob pais diferentes. Escolher um seria chute.
            report.conflicts.append(
                f"'{local_name}' é ambígua: {len(candidates)} categorias com esse nome; "
                f"'{pluggy_description}' não foi mapeada"
            )
            continue

        local = candidates[0]
        if str(local.id) in claimed:
            # Duas categorias da Pluggy disputando a mesma nossa.
            # `pluggy_category_id` é uma coluna só, então uma sobrescreveria a
            # outra — e quem venceria dependeria da ordem de iteração do
            # dicionário. Recusar todas e relatar é a única saída honesta com o
            # schema atual; ver a nota sobre N:1 no topo deste módulo.
            report.conflicts.append(
                f"'{local_name}' foi reivindicada por mais de uma categoria da "
                f"Pluggy (a última: '{pluggy_description}'); nenhuma foi gravada"
            )
            to_update.pop(str(local.id), None)
            continue
        claimed.add(str(local.id))

        if local.pluggy_category_id is None:
            to_update[str(local.id)] = pluggy_id
        elif local.pluggy_category_id == pluggy_id:
            report.already_mapped += 1
        else:
            report.conflicts.append(
                f"'{local_name}' já aponta para {local.pluggy_category_id}; "
                f"o de/para pede {pluggy_id}"
            )

    for category_id, pluggy_id in to_update.items():
        await session.execute(
            update(Category)
            .where(Category.id == category_id)
            .values(pluggy_category_id=pluggy_id)
        )
    report.mapped = len(to_update)

    mapped_descriptions = set(PLUGGY_TO_LOCAL)
    report.unmatched_pluggy = sorted(d for d in by_description if d not in mapped_descriptions)
    report.unmapped_local = sorted(
        row.name
        for row in local_rows
        if row.pluggy_category_id is None and str(row.id) not in to_update
    )

    if report.conflicts:
        logger.warning("de/para de categorias com %s conflito(s)", len(report.conflicts))
    return report
