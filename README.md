# Organizador Financeiro

SaaS de finanças pessoais com agregação via Open Finance (Pluggy), categorização
automática por LLM self-hosted (Ollama) e conformidade com a LGPD desde o schema.

**Estado atual: Fases 0 e 1 concluídas** — infraestrutura e modelagem de dados.
Nenhuma integração externa foi implementada ainda.

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

- Frontend: <http://localhost:3000> — painel de diagnóstico das três conexões
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
- [ ] **Fase 3** — Categorização via Ollama (`qwen3.5:9b`)
- [ ] **Fase 4** — Dashboard, filtros e tela de revisão de categorias
- [ ] **Fase 5** — LGPD: criptografia em repouso, retenção e registro de consentimento

O contexto já levantado para as Fases 3 a 5 — decisões tomadas, questões em
aberto e as armadilhas de cada uma — está em
[`docs/fases-3-5.md`](docs/fases-3-5.md). A referência da API Pluggy, com marcação
do que foi verificado na doc oficial e do que ainda precisa ser confirmado, está
em [`docs/pluggy-api.md`](docs/pluggy-api.md).

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
