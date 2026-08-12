# Organizador Financeiro — contexto para agentes

SaaS de finanças pessoais: agregação via Open Finance (Pluggy), categorização por
LLM self-hosted (Ollama), multi-tenancy com RLS e conformidade com a LGPD.
Uso pessoal, um único tenant ativo, schema já preparado para vários.

**Estado:** Fases 0 a 2 concluídas (infra, schema, integração Pluggy). Fase 3
(categorização por LLM) é a próxima. O roadmap completo e o racional das decisões
estão no [README](README.md).

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
  `categorization_status`, `category_confidence` nem `kind` quando
  `category_source = 'MANUAL'`;
- re-sync **ressuscita** linha soft-deleted (`deleted_at = NULL`);
- `kind` é derivado do sinal do valor quando o chamador não informa.

O caminho ponta a ponta (resposta da Pluggy → banco) está coberto em
`tests/test_pluggy_sync.py`, sob a conexão `app_user` — e não só via
`ingestion.py`, porque um sync com `INSERT` próprio passaria verde nos testes
daquele módulo e quebraria as três regras em silêncio.

### ⚠️ Armadilha esperando a Fase 3

`keep_if_manual` protege só `category_source = 'MANUAL'`. Para tudo mais, o
re-sync traz `categorization_status = 'PENDING'` de volta — inclusive em linhas
que o LLM já tiver categorizado.

Na Fase 2 isso é inócuo, porque nada categoriza ainda. **Na Fase 3 vira
reprocessamento do histórico inteiro a cada sync**, com custo de GPU e nenhum
sinal visível de que algo está errado. A correção é em `ingestion.py` (preservar
quando `category_source IS NOT NULL`, não só quando é `MANUAL`) e o lugar de
fazê-la é o começo da Fase 3.

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
  `tests/test_pluggy_pure.py`, `tests/test_pluggy_mappers.py` ou
  `tests/test_config.py`.
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

## Fases 3 a 5

Contexto acumulado em [`docs/fases-3-5.md`](docs/fases-3-5.md): o que já está
decidido e o que segue em aberto na categorização por LLM, no frontend e na LGPD.
**Nada disso está implementado** — leia antes de projetar algo que já tem decisão
tomada, mas não comece uma fase seguinte sem o usuário pedir.

Três destaques, porque são fáceis de errar sem ler:

- o Ollama aceita **JSON Schema** no parâmetro `format`, então a categoria é saída
  restrita por schema, não texto a ser parseado;
- ao categorizar, o `kind` tem de ser herdado de `categories.kind` junto com a
  categoria — esquecer deixa transferência contando como gasto;
- **não existe autenticação**; `X-Tenant-Id` é confiado cegamente e só há recusa
  quando `ENVIRONMENT=production`.

## Fora de escopo no MVP

Plano comercial da Pluggy · pipeline híbrida (regras + embeddings) · multi-tenancy
real com vários usuários pagantes — só o schema preparado.
