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
- [ ] **Fase 2** — Integração Pluggy: conexão de conta, sync de transações,
      avaliação da categorização nativa
- [ ] **Fase 3** — Categorização via Ollama (`qwen3.5:9b`)
- [ ] **Fase 4** — Dashboard, filtros e tela de revisão de categorias
- [ ] **Fase 5** — LGPD: criptografia em repouso, retenção e registro de consentimento

O contexto já levantado para as Fases 3 a 5 — decisões tomadas, questões em
aberto e as armadilhas de cada uma — está em
[`docs/fases-3-5.md`](docs/fases-3-5.md). A referência da API Pluggy, com marcação
do que foi verificado na doc oficial e do que ainda precisa ser confirmado, está
em [`docs/pluggy-api.md`](docs/pluggy-api.md).

### Decisões já tomadas para a Fase 2

**Sync sob demanda, não webhook nem polling agendado.** A sincronização dispara
quando o app é atualizado. Isso dispensa endpoint público (que em `localhost`
exigiria túnel) e dispensa cron. Duas consequências a tratar na implementação:

- precisa de *throttle* por `bank_connections.last_synced_at`, senão dois F5
  seguidos disparam duas atualizações do item na Pluggy;
- a atualização de um item leva de segundos a minutos, então o request não pode
  bloquear a tela — o endpoint devolve o estado atual e agenda o refresh.

**Cartão de crédito: despesa na data da compra.** O pagamento da fatura entra
como `TRANSFER`, então os mesmos valores não contam duas vezes. Falta confirmar,
com dado real, o sinal que os connectors devolvem em conta de crédito — alguns
invertem. O `raw_payload` guarda a resposta original, então é reprocessável.

**Como o `item` é criado?** `POST /connect_token` exige apenas a API key, então o
widget Pluggy Connect deve funcionar no tier pessoal — mas há relatos de que a
configuração de connectors trava após o trial do Dashboard, e o fluxo documentado
para uso pessoal é conectar em `meu.pluggy.ai` e copiar o `itemId`. A Fase 2 vai
suportar os dois caminhos; o schema atual já atende a ambos sem alteração.

**A verificar na primeira chamada real:** se o tier gratuito permite disparar a
atualização de um item (`PATCH /items/{id}`). Se não permitir, a frequência de
atualização é ditada pela Pluggy e o "sync ao abrir o app" vira apenas leitura do
que já foi sincronizado do lado deles.
