# Fases 3 a 5 — contexto acumulado

Notas levantadas enquanto as Fases 0 e 1 eram construídas. **Nada aqui está
implementado.** A execução continua uma fase por vez; este documento existe para
que a decisão tomada na Fase 1 não precise ser redescoberta na Fase 4.

Convenção: ✅ **decidido** (e por quê) · ⚠️ **em aberto** (precisa de decisão ou
de dado real).

---

## Fase 3 — Categorização via LLM

### A fila já existe no schema

✅ `ix_transactions_pending` é um índice **parcial**
(`WHERE categorization_status = 'PENDING'`). A consulta da fila é
`WHERE tenant_id = ? AND categorization_status = 'PENDING' ORDER BY date`, e o
índice encolhe conforme o backlog é processado em vez de crescer com a tabela.

Ao categorizar, o job escreve cinco campos de uma vez:
`category_id`, `category_source = 'LLM'`, `categorization_status`,
`category_confidence`, `categorized_at` — **e `kind`, herdado de
`categories.kind`**. Esquecer o `kind` deixa um Pix classificado como
"Transferências" ainda contando como gasto no dashboard.

### Saída estruturada por JSON Schema, não por parsing

✅ O Ollama aceita um **JSON Schema completo** no parâmetro `format` (não apenas
`"json"`), e o modelo é obrigado a produzir saída que casa com o schema:

```jsonc
POST http://host.docker.internal:11434/api/chat
{
  "model": "qwen3.5:9b",
  "messages": [ ... ],
  "stream": false,
  "options": { "temperature": 0 },
  "format": {
    "type": "object",
    "properties": {
      "category": { "type": "string" },
      "confidence": { "type": "number" }
    },
    "required": ["category", "confidence"]
  }
}
```

Fonte: <https://docs.ollama.com/capabilities/structured-outputs>. Em Python dá
para gerar o schema a partir de um modelo Pydantic (`model_json_schema()`) e
reusar o mesmo modelo para validar a resposta. Temperatura 0 é a recomendação da
própria documentação para extração determinística.

⚠️ Falta confirmar na prática que o `qwen3.5:9b` respeita bem o schema — é um
teste de dez minutos com o modelo local, e vale fazer antes de desenhar o resto.

### Validar contra a lista real de categorias

✅ Schema garante *forma*, não *conteúdo*: o modelo continua livre para devolver
`"Alimentação e bebidas"` quando a categoria cadastrada é `"Alimentação"`. Toda
resposta precisa ser resolvida contra as categorias do tenant, e o que não casar
vira `categorization_status = 'NEEDS_REVIEW'` — não `FAILED`, que é para erro de
infraestrutura.

Uma alternativa mais forte, a avaliar: colocar a enumeração das categorias
válidas dentro do próprio schema (`"enum": [...]`), o que torna a resposta
inválida impossível por construção. Custa tokens no prompt e precisa ser
regenerado quando o usuário cria categoria.

### `category_confidence` merece ceticismo

⚠️ A coluna existe (`NUMERIC(4,3)`, CHECK entre 0 e 1), mas **confiança
autodeclarada por LLM é mal calibrada** — modelos dizem 0.95 com a mesma
facilidade com que acertam e erram. Usar esse número cru como gatilho de
`NEEDS_REVIEW` vai produzir uma fila de revisão que não corresponde aos erros
reais.

Três caminhos, a decidir com dado real:

1. tratar o valor do modelo como sinal grosseiro (só alto/baixo), não contínuo;
2. derivar confiança da **concordância** entre a categoria nativa da Pluggy
   (`pluggy_category_id`) e a do LLM — divergência entre duas fontes
   independentes é sinal melhor que a autoavaliação de uma;
3. medir a calibração de verdade: registrar as previsões, esperar as correções
   manuais do usuário e comparar. É o único caminho honesto, e só funciona depois
   de haver volume.

### Concorrência

⚠️ O Ollama serializa requisições por padrão. Com GPU dedicada, uma chamada por
transação é viável (a decisão do usuário), mas **paralelismo alto não acelera** —
só enfileira. Começar com concorrência 1 e medir antes de complicar.

O job precisa ser **retomável**: uma queda no meio do backlog não pode perder o
que já foi feito nem reprocessar tudo. Como cada transação é atualizada
individualmente e a fila é definida por `categorization_status`, isso sai de
graça — desde que o job não abra uma transação de banco única para o lote inteiro.

### Ponto de extensão da pipeline híbrida

✅ `category_rules` existe vazia, com o formato já fixado
(`EXACT`/`CONTAINS`/`REGEX`/`AMOUNT_RANGE`, `priority`, `is_active`) e um índice
parcial de avaliação. `CategorySource` já prevê `RULE` e `EMBEDDING`.

A pipeline final é `regra → embedding → LLM`, com o LLM como último recurso. O
MVP implementa só o LLM, mas o `category_source` gravado precisa ser fiel desde
já — é ele que vai permitir medir, depois, quanto de cada camada ficou.

### Antes de codificar a Fase 3

✅ A Fase 2 tem que responder primeiro: **a categorização nativa da Pluggy já
basta?** É para isso que `pluggy_category_id` / `pluggy_category_name` guardam a
resposta crua. A medição é comparar a categoria da Pluggy com as correções
`category_source = 'MANUAL'` depois de algumas semanas de uso. Se o acerto for
alto, a Fase 3 encolhe para "LLM só nos casos que a Pluggy não classificou".

---

## Fase 4 — Frontend

### Três telas

Conexão de contas (widget Pluggy Connect **e** colar `itemId`) · dashboard com
filtros por período, categoria e conta · tela de revisão de categorização.

✅ **A tela de revisão é a mais importante das três**, e não por UX: é ela que
produz `category_source = 'MANUAL'`, que é simultaneamente a correção do usuário,
a base de regras da pipeline híbrida e a única régua para medir o acerto do LLM.
Atrito nessa tela custa o dado que as Fases 3 e 5 dependem. Ela também precisa
permitir ajustar o `kind`, não só a categoria.

### O dashboard soma por `kind` e agrupa por categoria

✅ Não some `amount` sem filtrar `kind`. `TRANSFER` fora dos totais de receita e
despesa — é a razão de o campo existir.

⚠️ Os índices atuais são `(tenant_id, account_id, date DESC)` e
`(tenant_id, date DESC)`. Um índice que inclua `kind` **não** foi criado de
propósito: índice custa escrita em todo sync, e sem a query real do dashboard
seria especulação. Medir com `EXPLAIN ANALYZE` na Fase 4 e só então decidir.

### Onde o sync é disparado

✅ A decisão da Fase 2 é sync sob demanda, ao atualizar o app. O gatilho mora
aqui. Duas restrições que a interface precisa respeitar: throttle por
`bank_connections.last_synced_at` (dois F5 não podem virar duas atualizações na
Pluggy) e não bloquear a tela, porque atualizar um item leva de segundos a
minutos. O estado da conexão vem de `status` / `execution_status`.

### Padrão de chamada já estabelecido

✅ `INTERNAL_API_URL` (`http://backend:8000`) para chamadas de Server Component,
que saem de dentro da rede do compose; `NEXT_PUBLIC_API_URL` (`localhost:8000`)
para o que o navegador chama. Ver `frontend/src/app/page.tsx`. `NEXT_PUBLIC_*` vai
embutido no bundle: nunca coloque segredo lá.

### ⚠️ Não existe autenticação

O tenant vem do header `X-Tenant-Id`, sem verificação, com fallback para o tenant
padrão. `resolve_tenant_id` (`app/core/tenancy.py`) recusa o request quando
`ENVIRONMENT=production`, então a lacuna não passa despercebida num deploy — mas
**qualquer exposição para fora de localhost exige autenticação antes**. O schema
já tem `users` com `hashed_password`, sem nada implementado em cima.

---

## Fase 5 — LGPD e segurança

### O que realmente precisa de criptografia

⚠️ `APP_ENCRYPTION_KEY` (chave Fernet) está no `.env.example` e **não é usada por
nada** hoje. Vale escopar com honestidade antes de implementar: as credenciais
bancárias do usuário **nunca passam por este sistema** — ficam na Pluggy. O que
existe de sensível aqui é menos do que o brief sugere:

- `PLUGGY_CLIENT_SECRET` — está no `.env`, não no banco;
- tokens da Pluggy, **se** forem cacheados em banco (a API key dura ~2h; cachear
  em memória evita o problema por completo);
- `taxNumber` e `owner` (CPF e nome do titular), que hoje são **deliberadamente
  não persistidos**. Se a Fase 4 precisar deles, é aí que a criptografia em
  repouso passa a ter objeto.

Conclusão provável: a Fase 5 é mais sobre consentimento, retenção e exclusão do
que sobre criptografia.

### Direito de eliminação já está pronto

✅ Toda tabela tem `tenant_id` com `ON DELETE CASCADE` para `tenants`. Apagar o
titular é `DELETE FROM tenants WHERE id = ?` — não uma sequência manual que
alguém esquece de atualizar quando uma tabela nova aparecer. Foi decidido assim
na Fase 1 exatamente por causa do art. 18, VI.

⚠️ **`deleted_at` não é eliminação.** Soft delete existe para "sumiu na origem", e
o re-sync desfaz. Pedido de eliminação do titular tem que ser exclusão física.

### Consentimento

✅ `bank_connections` já tem `consent_granted_at` e `consent_expires_at`. O
consentimento do Open Finance expira (~12 meses) e a conexão morre junto — a
interface precisa avisar antes, não depois.

⚠️ Falta o registro formal: a que exatamente o titular consentiu, em que versão
dos termos, com qual evidência. Provavelmente uma tabela `consents` própria, já
que o consentimento sobrevive à conexão que o originou.

### Faltando

⚠️ **Portabilidade** (art. 18, V): exportar os dados do titular em formato
legível. Não existe nada.

⚠️ **Retenção**: por quanto tempo guardar transações depois de o usuário
desconectar o banco? Sem política definida, o default é "para sempre", que é
exatamente o que a LGPD não quer.

⚠️ **Trilha de auditoria**: quem acessou o quê. Não modelado. Só faz sentido com
autenticação real.

---

## Pendências que atravessam as fases

| Item | Onde |
|---|---|
| Autenticação inexistente (`X-Tenant-Id` é confiado cegamente) | bloqueia qualquer exposição pública |
| `dedup_seq` numerado por lote quebra em extratos com sobreposição parcial | só importa se OFX/CSV virar caminho principal |
| `test_every_tenant_table_has_rls_enabled` itera `TENANT_SCOPED_TABLES` — tabela nova fora da lista escapa | melhoraria detectando qualquer tabela com coluna `tenant_id` |
| Sinal invertido em conta de crédito por alguns connectors | confirmar na primeira sync real (Fase 2) |
| Tier gratuito permite disparar atualização de item? | confirmar na primeira chamada real (Fase 2) |
