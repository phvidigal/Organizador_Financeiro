# Organizador Financeiro

SaaS de finanças pessoais com agregação via Open Finance (Pluggy), categorização
automática por LLM self-hosted (Ollama) e conformidade com a LGPD desde o schema.

**Estado atual: Fases 0 a 4 concluídas** — infraestrutura, modelagem, integração
com a Pluggy, categorização por LLM, fila de revisão e dashboard. Falta a Fase 5
(LGPD).

## Stack

| Camada | Tecnologia |
|---|---|
| Backend | Python 3.12 · FastAPI · SQLAlchemy 2.0 (async) · Alembic |
| Banco | PostgreSQL 16, com Row-Level Security por tenant |
| Frontend | Next.js 15 (App Router) · TypeScript |
| Agregação | Pluggy — tier pessoal "Meu Pluggy" |
| Categorização | Ollama + `qwen3.5:9b`, rodando no host |

## Como rodar

Pré-requisitos: Docker Desktop e Ollama instalados. Node e Python **não** precisam
estar no host — ambos rodam em container.

```bash
cp .env.example .env
```

Ajuste o `.env` — no mínimo `APP_DB_PASSWORD`, e a mesma senha dentro de
`DATABASE_URL`. As credenciais da Pluggy só são necessárias a partir da Fase 2.

> O role `app_user` é criado pelo entrypoint do Postgres, que só roda com o
> volume vazio. Trocar `APP_DB_PASSWORD` depois do primeiro `up` exige
> `ALTER ROLE app_user WITH PASSWORD '...'` ou um `docker compose down -v`.

Depois:

```bash
docker compose up -d --build
```

Aplique as migrations:

```bash
docker compose exec backend alembic upgrade head
```

- Frontend: <http://localhost:3000> — dashboard. As demais telas são
  `/transacoes`, `/revisao`, `/conexoes` e `/diagnostico` (a saúde das três pontas)
- API: <http://localhost:8000/docs>

Testes:

```bash
docker compose exec backend pytest -v
```

## Arquitetura

```
Pluggy Connect ──► FastAPI ──► PostgreSQL
                      │             ▲
                      │             │
                      └──► Ollama ──┘
                        (categorização)
                            ▲
                     Next.js (dashboard)
```

## Decisões de modelagem

O detalhamento está nos comentários de [`backend/app/models/`](backend/app/models/)
e da [migration inicial](backend/alembic/versions/). Em resumo:

**Multi-tenancy com RLS.** Toda tabela tem `tenant_id`, e o PostgreSQL impõe o
isolamento por policy — não a aplicação. Um `WHERE` esquecido numa query retorna
vazio em vez de vazar dado de outro tenant. A aplicação conecta como `app_user`,
que não é dono das tabelas; o owner (usado só pelas migrations) ignora RLS por
design. Se a aplicação conectasse como owner, o RLS não bloquearia nada.

E como essa diferença é invisível em runtime — o app funcionaria perfeitamente
enxergando todos os tenants —, o backend verifica no startup se o role da conexão
pode ignorar as policies e **se recusa a subir** se puder
([`rls_guard.py`](backend/app/core/rls_guard.py)). Trocar `DATABASE_URL` para o
owner numa depuração e esquecer de voltar é o cenário realista; um container que
não sobe custa dez minutos, isolamento desligado em silêncio pode levar meses
para ser notado.

**Valores em `NUMERIC(18,2)`.** Nunca float. Ponto flutuante binário não
representa `0,10` exatamente e o erro acumula em soma de extrato.

**Deduplicação em duas constraints, não uma.** A natureza do identificador muda
com a origem:

- *Pluggy e OFX* têm identificador estável (UUID, FITID) → índice parcial único em
  `(tenant_id, source, external_id)`, e a gravação é **upsert**, porque transação
  da Pluggy muda depois de criada (`PENDING` → `POSTED`, valor corrigido).
- *CSV e entrada manual* não têm identificador. O óbvio seria
  `UNIQUE(conta, data, valor, descrição)` — que **rejeita dado legítimo**: dois
  cafés de R$ 12,00 no mesmo dia são duas transações reais. A solução é
  `dedup_hash` (sha256 dos campos normalizados) somado a `dedup_seq`, que numera
  ocorrências repetidas dentro do lote. Reimportar o mesmo arquivo regenera os
  mesmos pares e colide; repetições genuínas recebem seq distintos e entram.

**`kind` separado da categoria.** `INCOME` / `EXPENSE` / `TRANSFER` é campo
próprio, não uma categoria. No Brasil, Pix e pagamento de fatura dominam o extrato
sem serem gasto: sem esse campo, um Pix de R$ 2.000 entre contas do próprio
titular apareceria como R$ 2.000 de despesa numa conta e R$ 2.000 de receita na
outra. É também o que torna seguro contar cada compra do cartão como despesa na
data da compra — o pagamento da fatura entra como `TRANSFER` e não soma de novo.

A categoria declara seu `kind` (tudo sob "Transferências" é `TRANSFER`), e a
transação herda ao ser categorizada. Na ingestão, o `kind` sai do sinal do valor;
`TRANSFER` nunca é inferido automaticamente, porque nenhuma origem de dado diz
sozinha que o destino é conta do próprio titular. Até ser classificada, a
transferência conta como gasto — o erro oposto esconderia gasto real.

**O upsert nunca sobrescreve categoria manual — nem `kind` manual.** A correção
manual do usuário é a base de regras e de treino das Fases 3 e 4. Uma
re-sincronização apagá-la seria uma perda silenciosa.

**Re-sync ressuscita transação excluída.** `deleted_at` significa "sumiu na
origem"; se a instituição voltou a reportá-la, ela existe de novo, e escondê-la
deixaria o extrato com um saldo que não fecha. Isso vale enquanto o soft delete
tiver esse único significado — no dia em que existir "excluir" na interface, a
exclusão do usuário precisa de coluna própria.

**Dois eixos de categorização.** `categorization_status` ("já processei?") e
`category_source` ("quem decidiu: Pluggy, regra, LLM ou humano?"). Separados
porque é o que permite medir se a categorização nativa da Pluggy basta, e depois
comparar o acerto do LLM contra a correção manual.

## Estrutura

```
backend/     FastAPI, modelos, migrations e testes
frontend/    Next.js
infra/       Script de inicialização do Postgres (cria o role da aplicação)
legacy/      Protótipo SQLite original, apenas como referência
```

## Roadmap

- [x] **Fase 0** — Docker Compose, `.env.example`, Alembic
- [x] **Fase 1** — Schema, RLS, constraints de deduplicação, seed de categorias
- [x] **Fase 2** — Integração Pluggy: conexão de conta, sync de transações,
      avaliação da categorização nativa
- [x] **Fase 3** — Categorização via Ollama (`qwen3.5:9b`), com saída restrita por
      JSON Schema e fila de revisão
- [x] **Fase 4** — Tela de revisão (`PATCH /transactions/{id}`,
      `categorization_reviews`) e dashboard com filtros por período, categoria e
      conta
- [ ] **Fase 5** — LGPD: criptografia em repouso, retenção e registro de consentimento

O contexto já levantado para as Fases 4 e 5 — decisões tomadas, questões em
aberto e as armadilhas de cada uma — está em
[`docs/fases-3-5.md`](docs/fases-3-5.md). A referência da API Pluggy, com marcação
do que foi verificado na doc oficial e do que ainda precisa ser confirmado, está
em [`docs/pluggy-api.md`](docs/pluggy-api.md).

### Como a Fase 3 categoriza

Uma chamada ao Ollama por transação, com a lista de categorias do titular
**dentro do JSON Schema** que vai no parâmetro `format`. O modelo não escreve um
nome de categoria: ele escolhe um item de um `enum`, e o Ollama restringe a
gramática da geração a ele. Resposta fora da taxonomia deixa de ser possível por
construção, em vez de ser um texto a ser adivinhado depois.

**A confiança do modelo não decide sozinha.** Confiança autodeclarada por LLM é
mal calibrada — 0,95 sai com a mesma facilidade no acerto e no erro. Então o
segundo sinal é a **discordância com a categorização nativa da Pluggy**: quando as
duas fontes independentes apontam para árvores diferentes, a transação vai para
`NEEDS_REVIEW` mesmo com confiança alta. É para isso que
`categories.pluggy_category_id` foi preenchido na Fase 2.

O número cru do modelo é gravado como veio, sem misturar a concordância dentro
dele. Combinar os dois sinais numa coluna só destruiria justamente o dado que a
Fase 4 precisa para medir a calibração real, comparando as previsões com as
correções manuais do usuário.

**Ao categorizar, o `kind` é herdado de `categories.kind`.** É o passo que tira o
pagamento de fatura e o Pix entre contas próprias do total de gastos — sem ele, a
categoria diz "Transferências" e o dashboard continua somando o valor como
despesa.

O job é retomável de graça: a fila é `categorization_status = 'PENDING'`, cada
transação é gravada na própria unidade de trabalho, e nenhuma chamada ao Ollama
acontece com transação de banco aberta. Uma queda no meio do backlog preserva o
que já foi feito. Concorrência é 1 de propósito — o Ollama serializa requisições,
então paralelismo não acelera, só enfileira.

Disparo:

```bash
curl -s -X POST "http://localhost:8000/categorization/run?limit=10"
```

`GET /categorization/status` acompanha, e `POST /categorization/reset` devolve à
fila as decisões do LLM — é o que torna barato ajustar o prompt e rodar de novo.
Ele recusa `source=MANUAL`: a correção do usuário é a régua para medir o acerto do
LLM, e nenhum caminho do sistema pode apagá-la. O sync da Pluggy encadeia a
categorização ao terminar, então transação nova não fica parada na fila.

### O que a primeira rodada real mostrou

🔬 O extrato inteiro — **333 transações, `qwen3.5:9b`, ~11 minutos, zero falhas.**
Cerca de 1,6 s por transação com o modelo quente. Resultado: 237 `CATEGORIZED`,
96 `NEEDS_REVIEW`.

**O balde da Pluggy rachou, que era a aposta da fase.** Papelaria, lanchonete e
compra de renda variável estavam todos sob `Shopping` e saíram para categorias
diferentes. E as duas pernas do pagamento de fatura — a saída na conta e a entrada
no cartão, de mesmo valor — viraram `TRANSFER`: os mesmos reais deixaram de contar
como despesa de um lado e receita do outro.

**O `kind` mudou de forma:** de 272 `EXPENSE` / 61 `INCOME` / **0** `TRANSFER`
para 176 / 13 / **144**. Cento e quarenta e quatro lançamentos saíram dos totais
de receita e despesa — que é a razão de o campo existir.

**Os dois sinais de revisão pesam quase igual:** dos 96 `NEEDS_REVIEW`, **51 vêm
de discordância com a Pluggy e 45 de confiança abaixo de 0,70**.

Vale registrar que uma amostra de dez transações havia sugerido o contrário. Nela
a confiança veio `0.950` em nove respostas, e a conclusão apressada foi que o
número era constante e o limiar, decorativo. Com 333, ele se distribui — 269 em
`0.950`, mas também 27 em `0.650`, 15 em `0.450`, 3 em `0.150`, 10 em `1.000`. O
limiar dispara, e responde por quase metade da fila de revisão. Dez transações não
eram amostra.

**A comparação por raiz se pagou.** Uma compra de ingressos recebeu
`Digital services` da Pluggy (→ *Streaming e assinaturas*) e `Eventos e cultura`
do LLM: folhas diferentes, mesma raiz `Lazer`. Comparar por folha teria mandado
um acerto para a revisão.

**A receita tinha sumido, e a taxonomia era a culpada.** Na primeira passada
`Receitas` ficou com 13 lançamentos — só rendimento de investimento, **menos de 1%
do dinheiro que de fato entrou no período**. Os outros 99% estavam em
`Transferências > Pix recebido`: 34 lançamentos, com parcelas mensais de valor
idêntico e pagamentos vindos de pessoa jurídica.

O modelo não errou: `Pix recebido` nasceu sob `Transferências` na Fase 1, e
`Transferências` é `TRANSFER`. Isso embute a premissa de que **todo** Pix recebido
é dinheiro andando entre contas do próprio titular — falsa para qualquer pessoa
que receba pagamento por Pix no Brasil.

A migration `0004` moveu `Pix recebido` para `Receitas` (`INCOME`), deixando
`Transferências > Transferência entre contas próprias` para o caso legítimo. Mas
nem todo Pix é renda, e a escolha entre as duas depende de saber quem é o
remetente — coisa que só o titular sabe. Então o prompt manda o modelo **baixar a
confiança quando não houver como decidir**: a confiança é o único canal que ele
tem para fazer uma pergunta, e confiança baixa põe a transação em `NEEDS_REVIEW`
em vez de cravar um palpite.

Depois da migration, com o backlog reprocessado:

| | antes | depois |
|---|---|---|
| lançamentos em `Receitas` | 13 | **45** |
| do que entrou, quanto era receita | <1% | **~100%** |
| `INCOME` / `EXPENSE` / `TRANSFER` | 13 / 176 / 144 | 45 / 185 / 103 |
| `NEEDS_REVIEW` | 96 | 137 |

Os 32 `Pix recebido` foram **todos** para a fila de revisão, e os 15
`Transferência entre contas próprias` passaram direto — o modelo cravou onde havia
evidência (mesmo nome no remetente) e perguntou onde não havia. A fila subiu de
96 para 137 porque ele passou a perguntar mais, que é o comportamento desejado: os
137 se dividem em **70 perguntas por confiança baixa e 67 discordâncias da
Pluggy**.

⏳ **O aprendizado ainda não fecha o laço.** A resposta do titular vira
`category_source = 'MANUAL'` e sobrevive a qualquer re-sincronização, e
`category_rules` já existe com o formato fixado — mas o LLM **não vê as correções
anteriores**. Realimentá-lo é a pipeline híbrida (`regra → embedding → LLM`), que
depende da tela de revisão da Fase 4 para haver o que aprender. Hoje há zero
correções manuais no banco.

**Faltava categoria para compra de investimento** — e a rodada foi o que revelou.
"Compra de Renda Variável" caía em `Outros` porque a taxonomia só tinha `Receitas >
Rendimentos e investimentos`, que é `INCOME`. O LLM não errou: não havia para onde
ir. A migration `0003` criou a árvore `Investimentos` (renda fixa, renda variável,
fundos, criptoativos, previdência), **com `kind = TRANSFER`**.

Aplicar não é gastar e resgatar não é receber: o principal continua sendo do
titular, só muda de conta. Fosse `EXPENSE`, um mês com R$ 5.000 aplicados
apareceria como R$ 5.000 de gasto — e o resgate desses mesmos R$ 5.000, meses
depois, como receita. O mesmo dinheiro contado duas vezes é precisamente o que o
campo `kind` existe para evitar.

O **rendimento** é outra coisa e continua onde estava: juros e dividendos são
dinheiro novo, e ficam em `Receitas > Rendimentos e investimentos` (`INCOME`). Essa
distinção é a parte frágil, e a regra 4 do prompt existe só para sustentá-la — na
rodada seguinte à migration, "Compra de Renda Variável" foi para
`Investimentos > Renda variável` (`TRANSFER`) e "Valor recebido de Investimentos"
continuou em `Rendimentos e investimentos` (`INCOME`), que era exatamente o risco.

> ⏳ **A calibração final ainda depende da Fase 4.** O limiar dispara e responde
> por metade da fila, mas se `0.450` de fato erra mais que `0.950` só as correções
> `MANUAL` acumuladas vão dizer.

### O que a Fase 2 descobriu

As três incógnitas que o planejamento deixou em aberto foram medidas contra a API
real, com credenciais do tier pessoal. Os detalhes técnicos estão em
[`docs/pluggy-api.md`](docs/pluggy-api.md), marcados com 🔬; o resumo do que mudou
no produto está aqui.

**O sync lê, não atualiza.** `PATCH /items/{id}` responde
`400 "MeuPluggy item cant be updated"`: no tier pessoal não existe "buscar agora
no banco". Quem dita a frequência é a Pluggy, que coleta sozinha a cada ~24h e
anuncia a próxima em `nextAutoSyncAt` — daí a coluna
`bank_connections.next_auto_sync_at` e a tela dizer "próxima coleta às HH:MM" em
vez de prometer atualização sob demanda. O throttle de dez minutos continua
existindo, agora por um motivo mais simples: sincronizar mais que isso relê
exatamente o mesmo conteúdo.

**O sinal do cartão é invertido — e o `type` já resolvia.** A conta de depósito
reporta compra como valor negativo; o cartão reporta a mesma operação como
positivo. Era exatamente a inversão que se temia, mas ela não chega ao banco
porque a normalização decide pelo campo `type` (`DEBIT`/`CREDIT`), não pelo sinal
recebido — confirmado comparando as duas contas do mesmo item, com a grande
maioria das transações do cartão caindo como saída (estornos e o pagamento da
fatura são a exceção esperada).

**O `itemId` é digitado, e não por preguiça:** `GET /items` não permite listagem
(responde 401 sempre), então não há como o backend descobrir quais conexões
existem. `POST /connect_token` **funciona** no tier pessoal — o widget Pluggy
Connect continua viável, e ficou fora da Fase 2 por escopo, não por impedimento.

**A categorização nativa da Pluggy cobre bem, mas acerta menos do que cobre.**
Testado contra um extrato real, ela classificou todas as transações sincronizadas,
mas uma categoria genérica ("Shopping") funciona como balde — absorve compra de
investimento, recarga de transporte e qualquer coisa cuja descrição comece com
"Compra no débito", sem relação com o que de fato foi comprado. A Fase 3 tem
trabalho, e agora com régua — `pluggy_category_id` está gravado em toda transação,
e o de/para em `app/services/pluggy/category_map.py` liga a maior parte das
nossas categorias à taxonomia deles.

**Cartão de crédito: despesa na data da compra.** O pagamento da fatura entra
como `TRANSFER`, então os mesmos valores não contam duas vezes. Isso depende de
categorização: até uma transferência ser classificada, ela conta como gasto (ver
a invariante do `kind` no `CLAUDE.md`). É por isso que a tela de transações da
Fase 2 não mostra totais — seriam números errados com cara de certos.

### Limitações conhecidas da Fase 2

- **Exclusão na origem não é detectada.** A Pluggy só reporta remoção via webhook,
  que está fora do desenho. Nenhuma transação é marcada como excluída pelo sync.
- **Um worker só.** O lock que impede dois syncs simultâneos da mesma conexão vive
  na memória do processo. Com `uvicorn --workers > 1` ele deixa de valer; o upsert
  torna a corrida inofensiva, mas ela dobraria a cota consumida na Pluggy.
- **O de/para de categorias é 1:1**, porque `categories.pluggy_category_id` é uma
  coluna só. Onde a taxonomia da Pluggy é mais fina que a nossa, a categoria fica
  sem mapeamento em vez de ganhar um mapeamento parcial.
