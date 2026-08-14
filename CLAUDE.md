# Organizador Financeiro — contexto para agentes

SaaS de finanças pessoais: agregação via Open Finance (Pluggy), categorização por
LLM self-hosted (Ollama), multi-tenancy com RLS e conformidade com a LGPD.
Uso pessoal, um único tenant ativo, schema já preparado para vários.

**Estado:** Fases 0 a 4 concluídas (infra, schema, integração Pluggy, categorização
por LLM, revisão e dashboard). Fase 5 (LGPD) é a próxima. O roadmap completo e o
racional das decisões estão no [README](README.md).

Idioma: código e schema em inglês; comentários, docs e conteúdo de usuário em
pt-BR. Commits em pt-BR.

---

## Invariantes — quebrar qualquer uma destas é regressão

### 1. Gravação de transação passa por `app/services/ingestion.py`

**Nunca escreva `INSERT INTO transactions` direto.** Use
`upsert_external_transactions` (Pluggy, OFX) ou `insert_hashed_transactions`
(CSV, manual).

Essas funções carregam três regras que não estão em constraint nenhuma:

- re-sync **não** sobrescreve `category_id`, `category_source`,
  `categorization_status`, `category_confidence` nem `kind` de linha que já tem
  `category_source` (`keep_if_decided`, qualquer origem — não só `MANUAL`);
- re-sync **ressuscita** linha soft-deleted (`deleted_at = NULL`);
- `kind` é derivado do sinal do valor quando o chamador não informa.

A primeira regra vale para **qualquer** `category_source`, e não só `MANUAL`,
porque `map_transaction` não emite as colunas de categoria: o `excluded.<col>`
delas vale NULL e o status volta como `PENDING`. Com o predicado restrito a
`MANUAL`, cada coleta automática da Pluggy — a cada ~24h — devolveria o histórico
inteiro para a fila, queimando GPU para reproduzir a mesma resposta, sem erro e
sem log.

A contrapartida importa: linha com `category_source IS NULL` **volta** para
`PENDING`. É como uma transação marcada `FAILED` (Ollama fora do ar) se
re-enfileira sozinha — e é por isso que `store.apply_decision` deixa
`category_source` NULL nesse caso, e só nesse.

O caminho ponta a ponta (resposta da Pluggy → banco) está coberto em
`tests/test_pluggy_sync.py`, sob a conexão `app_user` — e não só via
`ingestion.py`, porque um sync com `INSERT` próprio passaria verde nos testes
daquele módulo e quebraria as três regras em silêncio.

### 1b. Gravação de categorização passa por `app/services/categorization/store.py`

`apply_decision` grava seis colunas de uma vez: `category_id`, `category_source`,
`categorization_status`, `category_confidence`, `categorized_at` e **`kind`**. A
última é a que se esquece, e é a que importa para o dashboard — sem herdá-la de
`categories.kind`, um pagamento de fatura classificado como "Transferências"
continua contando como gasto.

`apply_manual_decision` é a irmã, para a correção do titular: mesmas seis colunas,
`category_source = 'MANUAL'`, status sempre `CATEGORIZED` e
**`category_confidence = NULL`**.

Zerar a confiança só é seguro porque o chamador grava **antes** uma linha em
`categorization_reviews` com `previous_confidence`. É de lá que a calibração se lê,
não de `transactions.category_confidence`. Quem chamar `apply_manual_decision` sem
registrar a revisão destrói exatamente o dado que a tela existe para produzir — e
não há erro nem log, só uma medição que nunca fecha. O único chamador é
`PATCH /transactions/{id}`, e ele faz as duas coisas na mesma transação.

`reset_categorization` é o caminho inverso, e recusa `MANUAL`.

### 2. Acesso a dado de tenant passa por `get_tenant_session`

`app/core/tenancy.py`. Ela abre transação e emite `SET LOCAL app.tenant_id`, que
é o que ativa as policies de RLS. Uma sessão obtida por `get_session` não enxerga
linha nenhuma — falha segura, mas falha.

A aplicação conecta como `app_user` (sujeito a RLS); migrations usam o owner via
`ALEMBIC_DATABASE_URL`. **Não troque `DATABASE_URL` para o owner**: o backend
verifica isso no boot e se recusa a subir (`app/core/rls_guard.py`).

Criou tabela nova com `tenant_id`? Precisa de `ENABLE ROW LEVEL SECURITY` + policy
na migration **e** do nome em `TENANT_SCOPED_TABLES` (`app/models/__init__.py`),
senão ela escapa do teste de isolamento.

### 3. Dinheiro é `NUMERIC(18,2)`, nunca float

Sinal normalizado na ingestão: negativo = saída, sempre. A resposta crua da
origem fica em `raw_payload`.

### 4. `kind` é separado de categoria

`INCOME` / `EXPENSE` / `TRANSFER`. O dashboard soma por `kind` e agrupa por
categoria. `TRANSFER` **nunca** é inferido automaticamente — depende de saber que
o destino é conta do próprio titular. Até ser classificada, transferência conta
como gasto (o erro oposto esconderia gasto real).

Categoria declara seu `kind`; a transação herda ao ser categorizada.

### 5. Migration e modelos não podem divergir

A migration inicial foi escrita à mão. Antes de commitar mudança de schema:

```bash
docker compose exec backend alembic revision --autogenerate -m "check"
```

O `upgrade()` gerado tem de ser `pass`. Se não for, o modelo e o banco
divergiram — corrija e **apague o arquivo gerado**.

Declarou `default=` num modelo? Declare `server_default=` junto, senão o
autogenerate propõe dropar o default do banco e todo `INSERT` em SQL cru passa a
violar `NOT NULL`.

---

## Comandos

```bash
docker compose up -d --build
```

```bash
docker compose exec backend alembic upgrade head
```

```bash
docker compose exec backend pytest -q
```

```bash
docker compose exec backend ruff check . --fix
```

- Frontend <http://localhost:3000> · API <http://localhost:8000/docs>
- `psql`: `docker compose exec db psql -U postgres -d finance`

## Particularidades do ambiente

- **Docker Desktop** pode não estar rodando; precisa ser iniciado à mão no Windows.
- **Node não está instalado no host.** O frontend só roda em container.
- **Ollama roda fora do Docker**, alcançado em `host.docker.internal:11434`.
  Modelo: `qwen3.5:9b`. Há GPU dedicada — pode chamar uma vez por transação.
  Diagnóstico: `GET /health/ollama`.
- **`.env` é gitignored** e contém a senha real do banco. `.env.example` tem
  placeholders. Nunca commite o primeiro.
- **Novo módulo de teste com `async def`** precisa de
  `pytestmark = pytest.mark.asyncio(loop_scope="session")` no topo, senão o
  asyncpg quebra com "attached to a different loop". Teste síncrono não pode
  ficar num módulo com esse marcador — use `tests/test_ingestion_pure.py`,
  `tests/test_pluggy_pure.py`, `tests/test_pluggy_mappers.py`,
  `tests/test_categorization_pure.py` ou `tests/test_config.py`.
- **`docker compose restart` não relê o `.env`.** As variáveis são injetadas na
  criação do container; depois de editar o arquivo é
  `docker compose up -d --force-recreate backend`. E `get_settings()` é
  `lru_cache`, então mesmo dentro do processo não há recarga.
- **Nos binds do asyncpg, use tipos Python de verdade.** `'2026-08-01'` como
  string numa coluna `date` levanta `AttributeError: 'str' object has no
  attribute 'toordinal'` — o psycopg aceitaria, o asyncpg não. Data literal
  *dentro* da string SQL funciona; como parâmetro, não.
- **Mock de HTTP é `httpx.MockTransport`**, que já vem no httpx. Não adicione
  `respx` nem `pytest-httpx`: o `AsyncClient` injetável que ele exige é o mesmo
  mecanismo que o teste ponta a ponta do sync usa.

---

## Fase 2 — como a integração Pluggy funciona

Referência de API em [`docs/pluggy-api.md`](docs/pluggy-api.md) — **não trabalhe
de memória**: o que foi medido contra a API real está marcado com 🔬 lá, e é onde
os palpites de planejamento foram corrigidos.

Três camadas, com fronteira rígida porque cada uma muda por um motivo diferente:

| Módulo | Muda quando | Conhece |
|---|---|---|
| `pluggy/client.py` | a API da Pluggy muda | httpx, settings |
| `pluggy/mappers.py` | o schema muda, ou a convenção de sinal | nada (puro) |
| `pluggy/sync.py` | a orquestração muda | client + mappers + ingestion |
| `pluggy/runner.py` | — | é a única parte que fala com o event loop |

O que é fácil de quebrar sem saber:

- **`upsert_external_transactions` olha só `rows[0].keys()`** para decidir o que o
  `ON CONFLICT` atualiza. Por isso `map_transaction` monta um literal com todas as
  chaves sempre, `None` onde não há dado, e tem um `assert` contra
  `TRANSACTION_ROW_KEYS`. Omitir chave faz coluna sumir do UPDATE em silêncio.
- **Nenhuma chamada HTTP acontece com transação de banco aberta.** Uma sessão por
  unidade de trabalho; a Pluggy é chamada entre elas. É o que torna o sync
  retomável e o que evita conexão `idle in transaction` por minutos.
- **O parse do JSON usa `parse_float=Decimal`** para dinheiro nunca virar float —
  e por isso `jsonable()` existe, para o `Decimal` conseguir entrar num JSONB.
- **`asyncio.create_task` precisa de referência forte** (`runner._tasks`), senão o
  GC pode coletar a task no meio e o sync para sem erro e sem log.
- **A validação de parâmetros da Pluggy é estrita**: mandar uma chave a mais na
  query derruba a chamada com 400. Não mande nada "por via das dúvidas".

O sync **lê, não atualiza**: `PATCH /items` é recusado no tier pessoal
(`REQUEST_REFRESH_BY_DEFAULT = False`). A frequência é da Pluggy, anunciada em
`bank_connections.next_auto_sync_at`.

### Endpoint temporário

`GET /pluggy/diagnostics` (`app/api/v1/pluggy_diagnostics.py`) despeja a resposta
crua da Pluggy ao lado do que seria gravado. Foi escrito para a validação da Fase
2 e continua útil para depurar connector novo. Bloqueado em produção; nunca
devolve valor de segredo, só nomes de campo.

### Limitações conhecidas

- **Exclusão na origem não é detectada.** A Pluggy só reporta remoção via webhook,
  fora do desenho. O sync nunca escreve `deleted_at` — e o upsert o *limpa*.
- **O lock de sync é por processo** (`runner._tasks`). Com `uvicorn --workers > 1`
  dois syncs simultâneos da mesma conexão passam a ser possíveis: inofensivos
  graças ao upsert, mas dobram a cota consumida.
- **O de/para de categorias é 1:1**, porque `categories.pluggy_category_id` é uma
  coluna só. Onde a taxonomia deles é mais fina, a categoria fica sem mapeamento.
- **`dedup_seq` é numerado por lote** (`app/services/ingestion.py`). Reimportar o
  mesmo arquivo é no-op, mas dois extratos com sobreposição *parcial* podem
  descartar uma ocorrência legítima. Só importa se a importação OFX/CSV virar
  caminho principal — o sync da Pluggy não usa esse caminho.

---

## Fase 3 — como a categorização funciona

Uma chamada ao Ollama por transação, com a categoria restrita por JSON Schema.
Mesma fronteira de camadas da Fase 2, e pelo mesmo motivo:

| Módulo (`app/services/categorization/`) | Muda quando | Conhece |
|---|---|---|
| `client.py` | a API do Ollama muda | httpx |
| `catalog.py` | a taxonomia muda | SQLAlchemy (só leitura) |
| `prompt.py` | o prompt ou o schema mudam | nada (puro) |
| `decide.py` | a regra de `NEEDS_REVIEW` muda | nada (puro) |
| `store.py` | as colunas de categorização mudam | SQLAlchemy |
| `job.py` | a orquestração muda | todos acima |
| `runner.py` | — | é a única parte que fala com o event loop |

O que é fácil de quebrar sem saber:

- **O `enum` do schema é o que garante a categoria.** `build_schema` põe os
  rótulos do tenant dentro de `format`, e o Ollama restringe a gramática da
  geração a eles — resposta fora da taxonomia deixa de ser possível. Trocar por
  string livre reintroduz o parsing que este desenho existe para evitar.
- **Rótulo é qualificado** (`"Alimentação > Delivery"`). O índice único é
  `(tenant_id, parent_id, name)`, então nome cru pode ser ambíguo.
- **`kind` é herdado de `categories.kind`** ao gravar (ver invariante 1b).
- **Dois eixos de falha, e confundi-los custa caro.** `FAILED` é infraestrutura;
  `NEEDS_REVIEW` é conteúdo. Ollama fora do ar aborta a execução e deixa as linhas
  `PENDING` — marcar centenas como `FAILED` exigiria um reset para recuperar.
- **Uma unidade de trabalho por transação**, e nenhuma chamada HTTP com transação
  de banco aberta. É o que torna o job retomável.
- **Concorrência 1.** O Ollama serializa requisições por padrão: paralelismo não
  acelera, só enfileira.
- **A confiança gravada é a crua do modelo.** A concordância com a Pluggy entra na
  *decisão*, nunca no número — misturar destruiria o dado que a Fase 4 precisa
  para medir a calibração contra as correções `MANUAL`.

Endpoints: `POST /categorization/run` (202, `limit` opcional),
`GET /categorization/status`, `POST /categorization/reset` (`source`, recusa
`MANUAL`). O sync encadeia a categorização ao terminar em `SUCCESS` com escrita —
o gancho é `_chain_categorization` em `app/api/v1/connections.py`, e mora lá, e não
em `pluggy/runner.py`, porque aquele pacote não deve conhecer a Fase 3.

### Limitações conhecidas

- **Descrição corrigida na origem não invalida a categoria.** O re-sync preserva a
  decisão; reprocessar é `POST /categorization/reset`.
- **O lock é por processo**, chaveado por tenant. Mesma ressalva do sync com
  `uvicorn --workers > 1`.
- **Os dois sinais de `NEEDS_REVIEW` pesam quase igual — não remova nenhum.**
  🔬 Nas 333 transações reais: 51 vieram de discordância com a Pluggy, 45 de
  confiança abaixo de `LOW_CONFIDENCE = 0.70`. O `qwen3.5:9b` responde `0.950` na
  maioria (269 de 333), mas também `0.650`, `0.450` e `0.150` quando a descrição
  é genérica. Uma amostra de dez sugeriu que o número era constante; não era.
- **`Pix recebido` é `INCOME` e mora sob `Receitas`** (migration 0004), não sob
  `Transferências`. 🔬 Sob a raiz antiga, mais de 99% do dinheiro que entrava
  ficava fora dos totais e `Receitas` só via rendimento de investimento. O caso de
  conta própria tem casa separada:
  `Transferências > Transferência entre contas próprias`.
- **A confiança é o canal de pergunta do LLM.** Só o titular sabe quem é o
  remetente de um Pix, então a regra 3c do `SYSTEM_PROMPT` manda usar confiança
  abaixo de 0.5 quando a descrição não decide — o que joga a linha em
  `NEEDS_REVIEW` em vez de cravar. 🔬 Funciona: os 32 `Pix recebido` reais foram
  todos para revisão, e os 15 `Transferência entre contas próprias` (mesmo nome no
  remetente) passaram direto. Afrouxar essa regra transforma pergunta em palpite.
- **`Pix enviado` continua `TRANSFER`**, e é o espelho não resolvido: pagar alguém
  por serviço é despesa. A regra 3b manda preferir a categoria do que foi pago,
  mas quem sobra em `Pix enviado` sem motivo conhecido continua fora dos gastos.
- **`Investimentos` é `TRANSFER`, e a distinção com o rendimento é frágil.**
  Aplicar/resgatar move o principal (`Investimentos > …`, TRANSFER); juros e
  dividendos são dinheiro novo (`Receitas > Rendimentos e investimentos`, INCOME).
  A regra 4 do `SYSTEM_PROMPT` é o que separa as duas — mexer nela sem conferir
  "Valor recebido de Investimentos" transforma receita em transferência.

## Fase 4 — como a revisão e o dashboard funcionam

Concluída: fila de revisão, correção manual e dashboard.

`PATCH /transactions/{id}` (`app/api/v1/transactions.py`) é o único caminho que
grava `category_source = 'MANUAL'`. Ele faz duas escritas na mesma transação, e a
ordem não é negociável:

1. `INSERT` em `categorization_reviews` com o estado **anterior**
   (`previous_category_id`, `previous_kind`, `previous_source`, `previous_status`,
   `previous_confidence`);
2. `store.apply_manual_decision`, que sobrescreve.

Sem o passo 1, depois do UPDATE não dá para saber se o titular **confirmou** a
escolha do LLM ou a **corrigiu** — e é essa diferença, e só ela, que responde se
`0.450` erra mais que `0.950`.

O que é fácil de quebrar sem saber:

- **`categorization_reviews` não tem `UPDATE` concedido ao `app_user`** (migration
  `0005`). O log é append-only por privilégio, não por convenção: corrigir de novo
  acrescenta linha, nunca reescreve. Precisa da tabela em `TENANT_SCOPED_TABLES`,
  senão o teste de RLS não a olha.
- **Confirmar a sugestão também grava.** Uma tela que só registrasse discordância
  mediria erro com numerador sem denominador. O botão "Confirmar" existe para isso,
  e por isso custa um clique.
- **`GET /categories` reusa `load_catalog`**, o mesmo carregamento que monta o
  `enum` do JSON Schema do Ollama. É o que garante que o humano escolha da mesma
  lista que o modelo pôde escolher; duas listas montadas por caminhos diferentes
  divergiriam na primeira categoria criada, e a correção deixaria de ser
  comparável. Só categorias ativas — transação apontando para uma desativada mostra
  "categoria inativa" em vez de um nome inventado.
- **A regra de `NEEDS_REVIEW` não é recomputada no cliente.** A tela mostra a
  confiança e o palpite da Pluggy como fatos crus; o veredito vive em
  `decide.decide` e em nenhum outro lugar.
- **`kind` é editável na tela, separado da categoria.** O default é herdado de
  `categories.kind`; o override existe porque `Pix enviado` é TRANSFER mas pagar
  alguém por um serviço é despesa, e só o titular sabe qual dos dois é.
- **Corrigir também acontece em `/transacoes`**, e não só na fila: um erro do LLM
  com confiança 0,95 e concordância com a Pluggy nunca entra em `NEEDS_REVIEW`, e
  sem esse caminho ficaria sem conserto.

### O dashboard

`GET /dashboard/summary` (`app/api/v1/dashboard.py`) só consulta e monta; a
aritmética vive em `app/services/dashboard.py`, que não conhece o banco — mesma
fronteira de `catalog.build_catalog` × `catalog.load_catalog`, e pelo mesmo motivo:
a soma é o que precisa de teste, e testá-la não deveria exigir Postgres.

O que é fácil de quebrar sem saber:

- **`TRANSFER` fora de `income`, `expense` e `net`.** É a razão de o campo `kind`
  existir. Se vazar, o total continua parecendo plausível — só está errado, e
  ninguém percebe: aplicar R$ 5.000 num CDB vira R$ 5.000 de gasto, e o resgate
  dos mesmos R$ 5.000 vira receita meses depois.
- **`needs_review_total` viaja junto de cada `kind`, e não é opcional.** É o que
  permite ao cartão dizer quanto do número é palpite. 🔬 Hoje **99,3% da receita**
  (R$ 33.206 de R$ 33.441) está em `NEEDS_REVIEW`, porque a regra 3c do
  `SYSTEM_PROMPT` manda o modelo perguntar em vez de cravar em `Pix recebido`. Um
  total sem essa companhia seria um número errado com cara de certo.
- **O rótulo da categoria diz "aguardando confirmação", nunca "confirmado".**
  `CATEGORIZED` por LLM significa "o modelo não perguntou", que não é a mesma coisa
  que o titular ter respondido. A segunda coisa só se lê de `categorization_reviews`.
- **A contagem da fila é do período filtrado**, como todo o resto da resposta. A
  faixa de aviso diz "deste período" por isso — senão discordaria dos números logo
  abaixo dela.
- **Rótulo sai de `load_catalog`**, resolvido em Python pelo `category_id`. É a
  terceira leitura do mesmo catálogo (`enum` do Ollama, `GET /categories`,
  dashboard); um join com CTE recursiva daria o mesmo nome hoje e divergiria no dia
  em que a regra de rótulo mudasse num lugar só.
- 🔬 **Nenhum índice novo, e isso foi medido.** A agregação faz Seq Scan em 333
  linhas: 0,67 ms de execução contra 3,3 ms de planejamento. Índice com `kind`
  custaria escrita em todo sync sem ter o que melhorar. Revisitar acima de ~50 mil
  linhas ou se a execução passar de 50 ms.

Frontend em `frontend/src/`: `lib/api.ts` (cliente e tipos),
`components/category-picker.tsx` (o controle que grava), `components/nav.tsx`,
`app/page.tsx` (dashboard), `app/revisao/page.tsx`, `app/diagnostico/page.tsx`.
O alias `@/*` aponta para `src/`. Gráficos são barras em CSS — nenhuma dependência
além de `next`, `react` e `react-dom`.

## Fase 5

Contexto acumulado em [`docs/fases-3-5.md`](docs/fases-3-5.md): o que já está
decidido e o que segue em aberto no frontend e na LGPD. **Nada disso está
implementado** — leia antes de projetar algo que já tem decisão tomada, mas não
comece uma fase seguinte sem o usuário pedir.

Dois destaques, porque são fáceis de errar sem ler:

- a **tela de revisão** é a mais importante da Fase 4, e não por UX: é ela que
  produz `category_source = 'MANUAL'`, que é a única régua para medir o acerto do
  LLM. Precisa permitir ajustar o `kind`, não só a categoria;
- **não existe autenticação**; `X-Tenant-Id` é confiado cegamente e só há recusa
  quando `ENVIRONMENT=production`.

## Fora de escopo no MVP

Plano comercial da Pluggy · pipeline híbrida (regras + embeddings) · multi-tenancy
real com vários usuários pagantes — só o schema preparado.
