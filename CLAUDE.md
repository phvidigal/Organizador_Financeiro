# Organizador Financeiro — contexto para agentes

SaaS de finanças pessoais: agregação via Open Finance (Pluggy), categorização por
LLM self-hosted (Ollama), multi-tenancy com RLS e conformidade com a LGPD.
Uso pessoal, um único tenant ativo, schema já preparado para vários.

**Estado:** Fases 0 e 1 concluídas (infra + schema). Fase 2 (Pluggy) é a próxima.
O roadmap completo e o racional das decisões estão no [README](README.md).

Idioma: código e schema em inglês; comentários, docs e conteúdo de usuário em
pt-BR. Commits em pt-BR.

---

## Invariantes — quebrar qualquer uma destas é regressão

### 1. Gravação de transação passa por `app/services/ingestion.py`

**Nunca escreva `INSERT INTO transactions` direto.** Use
`upsert_external_transactions` (Pluggy, OFX) ou `insert_hashed_transactions`
(CSV, manual).

Essas funções carregam três regras que não estão em constraint nenhuma e que a
suíte de testes **não** protege no caminho do sync — os testes exercitam o módulo
diretamente, então um sync com `INSERT` próprio passa verde e quebra tudo em
silêncio:

- re-sync **não** sobrescreve `category_id`, `category_source`,
  `categorization_status`, `category_confidence` nem `kind` quando
  `category_source = 'MANUAL'`;
- re-sync **ressuscita** linha soft-deleted (`deleted_at = NULL`);
- `kind` é derivado do sinal do valor quando o chamador não informa.

Ao escrever o sync da Fase 2, adicione um teste que exercite o caminho
*ponta a ponta* (resposta da Pluggy → banco), não só `ingestion.py`.

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
  ficar num módulo com esse marcador — use `tests/test_ingestion_pure.py`.

---

## Fase 2 — o que já está decidido

Ver também a seção "Decisões já tomadas para a Fase 2" no README e a referência
de API em [`docs/pluggy-api.md`](docs/pluggy-api.md) (não trabalhe de memória:
a doc oficial está linkada lá).

- **Sync sob demanda**, disparado quando o app é atualizado. Não é webhook (que
  exigiria endpoint público) nem cron. Precisa de throttle por
  `bank_connections.last_synced_at` e de execução não-bloqueante — atualizar um
  item na Pluggy leva de segundos a minutos.
- **Cartão de crédito:** cada compra é despesa na data da compra; pagamento da
  fatura entra como `TRANSFER`. Confirmar com dado real o sinal que o connector
  devolve em conta de crédito — alguns invertem.
- **Criação do item:** suportar os dois caminhos — colar um `itemId` existente
  (o usuário já tem um, vindo de meu.pluggy.ai) e o widget Pluggy Connect. O
  schema atende aos dois sem alteração.
- **Antes de partir para o LLM**, avaliar se a categorização nativa da Pluggy já
  basta. É para isso que `pluggy_category_id` / `pluggy_category_name` guardam a
  resposta crua e que `category_source` existe.
- **A verificar na primeira chamada real:** se o tier gratuito ("Meu Pluggy")
  permite disparar atualização de item. Se não permitir, a frequência é ditada
  pela Pluggy e o sync vira só leitura.

### Limitação conhecida

`dedup_seq` é numerado por lote (`app/services/ingestion.py`). Reimportar o mesmo
arquivo é no-op, mas dois extratos com sobreposição *parcial* podem descartar uma
ocorrência legítima. O usuário pretende usar só Pluggy, então isso só importa se
a importação OFX/CSV virar caminho principal.

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
