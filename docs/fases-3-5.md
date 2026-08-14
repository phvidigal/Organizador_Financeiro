# Fases 3 a 5 — contexto acumulado

Notas levantadas enquanto as Fases 0 e 1 eram construídas. Este documento existe
para que a decisão tomada numa fase não precise ser redescoberta na seguinte.

Convenção: ✅ **decidido** (e por quê) · ⚠️ **em aberto** (precisa de decisão ou
de dado real) · 🛠️ **implementado** (onde, e o que mudou em relação ao plano).

**A Fase 3 foi construída** — a seção abaixo virou registro do que ficou de pé, e
não mais plano. As Fases 4 e 5 continuam sem implementação nenhuma.

---

## Fase 3 — Categorização via LLM · 🛠️ implementada

Código em `app/services/categorization/`. O resumo operacional está no
[README](../README.md) e as armadilhas no
[`CLAUDE.md`](../CLAUDE.md); o que segue é o histórico das decisões, com o que o
plano acertou e o que precisou mudar.

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

🛠️ As seis colunas viraram `store.apply_decision`, com uma exceção que o plano não
previa: **`category_source` fica NULL quando o resultado é `FAILED`**. Como o
upsert do re-sync preserva toda linha com `category_source IS NOT NULL`, gravar
`'LLM'` num fracasso congelaria a linha em `FAILED` para sempre. Com NULL, o
próximo sync a devolve para `PENDING` e ela é tentada de novo sozinha.

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

🛠️ `app/services/categorization/client.py` monta essa requisição, com dois
acréscimos que o plano não previa:

- **`"think": false`**. O qwen3 é modelo de raciocínio, e pensar antes de escolher
  um item de lista fechada só multiplica o tempo por transação. Como nem todo
  modelo honra o pedido, `prompt.parse_response` ainda remove um `<think>…</think>`
  do começo do conteúdo — sem isso o `json.loads` falha num corpo que, tirando o
  prefixo, estava perfeito.
- **`parse_float=Decimal`** também aqui, pelo mesmo motivo do cliente da Pluggy: a
  confiança vai para `NUMERIC(4,3)`, e `Decimal(0.85)` não é 0.85.

🔬 **Confirmado na primeira rodada real** (10 transações do extrato, 35 s, zero
falhas): o `qwen3.5:9b` respeitou o `enum` em todas as respostas — nenhuma caiu no
caminho de "rótulo fora da taxonomia". A suíte continua rodando com
`httpx.MockTransport` e dublês, que é o certo; a verificação contra o modelo é
manual e pontual.

### Validar contra a lista real de categorias

✅ Schema garante *forma*, não *conteúdo*: o modelo continua livre para devolver
`"Alimentação e bebidas"` quando a categoria cadastrada é `"Alimentação"`. Toda
resposta precisa ser resolvida contra as categorias do tenant, e o que não casar
vira `categorization_status = 'NEEDS_REVIEW'` — não `FAILED`, que é para erro de
infraestrutura.

🛠️ **A alternativa mais forte foi a escolhida**: as categorias vão no `enum` do
próprio schema (`prompt.build_schema`), o que torna a resposta inválida impossível
por construção. O custo de tokens é real mas pequeno — 50 rótulos —, e o schema é
gerado por tenant a cada execução, então categoria criada ou desativada entra e
sai sozinha na rodada seguinte.

🛠️ **Os rótulos são qualificados** (`"Alimentação > Delivery"`), decisão que o
plano não tinha. O índice único de categorias é `(tenant_id, parent_id, name)`,
então dois pais podem ter filhas de mesmo nome: com nomes crus, `"Manutenção"`
mapearia duas categorias e resolver a resposta viraria chute. De quebra, a
hierarquia chega ao modelo sem gastar uma linha de prompt.

A resolução tolerante (caixa e espaços) e o caminho de `NEEDS_REVIEW` para rótulo
desconhecido continuam existindo, como rede para um modelo que ignore o `format`.

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

🛠️ **1 e 2 juntos, com 3 preservado.** `decide.decide` manda para `NEEDS_REVIEW`
quando a confiança fica abaixo de `LOW_CONFIDENCE = 0.70` **ou** quando a raiz da
categoria escolhida diverge da que o de/para da Pluggy indicaria.

A comparação é por **raiz**, não por folha: `"Alimentação"` contra
`"Alimentação > Restaurantes e bares"` é diferença de granularidade, e mandar isso
para revisão encheria a fila de acertos — que é a forma mais rápida de a tela de
revisão deixar de ser usada. Categoria da Pluggy sem de/para é "sem contraparte",
não discordância.

O caminho 3 continua possível porque `category_confidence` guarda o número **cru**
do modelo: a concordância entra na decisão, nunca no número. Misturar os dois
sinais numa coluna só destruiria exatamente o dado necessário para medir a
calibração depois. A concordância continua derivável por join entre
`transactions.pluggy_category_id` e `categories.pluggy_category_id`.

🔬 **Os dois sinais pesam quase igual, e a amostra pequena mentiu.**

Nas 333 transações reais, os 96 `NEEDS_REVIEW` se dividem em **51 por discordância
com a Pluggy e 45 por confiança abaixo de 0,70**. A distribuição do número do
modelo:

| confiança | lançamentos |
|---|---|
| 1.000 | 10 |
| 0.980 | 5 |
| 0.950 | **269** |
| 0.850 | 4 |
| 0.650 | 27 |
| 0.450 | 15 |
| 0.150 | 3 |

Registro do erro, porque ele é instrutivo: uma amostra de **dez** transações havia
mostrado `0.950` em nove respostas, e a conclusão tirada dali foi que o número era
uma constante e o limiar, decorativo. Está errado. O `0.950` é a resposta modal —
o modelo a usa para tudo que considera conclusivo —, mas ele *desce* quando a
descrição é genérica, e é justamente aí que a revisão importa. Dez transações
amostravam o comportamento comum e nenhum dos casos difíceis.

Conclusão que sobrevive: **manter as duas fontes.** Cada uma pega metade da fila,
e nenhuma delas cobre a outra.

⚠️ O que continua em aberto é se a confiança está *bem calibrada* — se `0.450`
erra mais que `0.950`. Isso só as correções `MANUAL` da Fase 4 podem dizer.

### Concorrência

⚠️ O Ollama serializa requisições por padrão. Com GPU dedicada, uma chamada por
transação é viável (a decisão do usuário), mas **paralelismo alto não acelera** —
só enfileira. Começar com concorrência 1 e medir antes de complicar.

O job precisa ser **retomável**: uma queda no meio do backlog não pode perder o
que já foi feito nem reprocessar tudo. Como cada transação é atualizada
individualmente e a fila é definida por `categorization_status`, isso sai de
graça — desde que o job não abra uma transação de banco única para o lote inteiro.

🛠️ Concorrência 1, uma unidade de trabalho por transação, e nenhuma chamada HTTP
com transação de banco aberta — a mesma disciplina de `pluggy/sync.py`.

🛠️ **O que o plano não previa foi a distinção entre os dois tipos de falha.**
Ollama fora do ar não pode marcar transação como `FAILED`: seriam centenas de
linhas precisando de um reset manual para voltar à fila. O job conta falhas de
infraestrutura consecutivas, aborta na terceira e **deixa as linhas `PENDING`**.
`FAILED` fica para o erro inesperado de uma linha só, e `NEEDS_REVIEW` para
resposta que chegou mas não serve.

🛠️ E a peça que faltava para o job ser de fato retomável não estava no job:
`ingestion.keep_if_manual` preservava só `category_source = 'MANUAL'`, então cada
coleta automática da Pluggy devolveria o histórico inteiro para a fila. Virou
`keep_if_decided` (`category_source IS NOT NULL`) no primeiro commit da fase. A
armadilha estava anotada no `CLAUDE.md` desde a Fase 2 — e sem essa anotação teria
passado despercebida, porque não produz erro nenhum.

### Gatilho

🛠️ `POST /categorization/run` dispara em background e devolve 202; um segundo
disparo enquanto há execução recebe 409 (o lock é por tenant, em memória do
processo). O sync da Pluggy encadeia a categorização ao terminar em `SUCCESS` com
escrita — pelo gancho `_chain_categorization` em `app/api/v1/connections.py`, e
não dentro de `pluggy/runner.py`, para que o pacote da Pluggy siga sem saber que a
Fase 3 existe.

🛠️ `POST /categorization/reset` devolve à fila as decisões de uma origem
(`LLM` por padrão) e recusa `MANUAL`. Existe para a iteração de prompt: sem ele, a
única forma de reprocessar seria um `UPDATE` na mão — e quem o escreve esquece de
reverter o `kind`, deixando um Pix marcado como `TRANSFER` apontando para
categoria nenhuma.

### Ponto de extensão da pipeline híbrida

✅ `category_rules` existe vazia, com o formato já fixado
(`EXACT`/`CONTAINS`/`REGEX`/`AMOUNT_RANGE`, `priority`, `is_active`) e um índice
parcial de avaliação. `CategorySource` já prevê `RULE` e `EMBEDDING`.

A pipeline final é `regra → embedding → LLM`, com o LLM como último recurso. O
MVP implementa só o LLM, mas o `category_source` gravado precisa ser fiel desde
já — é ele que vai permitir medir, depois, quanto de cada camada ficou.

🛠️ Continua fiel: o job grava `'LLM'` e nada mais. Uma camada nova entra por
`store.apply_decision`, que é onde as seis colunas são escritas juntas — não por
um `UPDATE` próprio.

### Antes de codificar a Fase 3

✅ A Fase 2 tinha que responder primeiro: **a categorização nativa da Pluggy já
basta?** É para isso que `pluggy_category_id` / `pluggy_category_name` guardam a
resposta crua.

🛠️ **A resposta foi "não, mas ajuda"**, e ela mudou o desenho. A Pluggy classifica
100% das transações, mas com categorias-balde: `Shopping` absorveu 50 das 333
linhas, incluindo compra de investimento e recarga de transporte. Adotar o palpite
dela onde há de/para teria herdado o balde inteiro.

Então o LLM decide **todas** as pendentes, e a categoria da Pluggy entra no prompt
como pista explicitamente falível ("o palpite do agregador costuma usar categorias
genéricas como balde; discorde dele quando a descrição indicar outra coisa") e na
decisão como sinal de confiança. A medição contra as correções `MANUAL` continua
valendo — agora para as duas fontes, não só para a Pluggy.

Um número que vale registrar como linha de base: antes da Fase 3, **zero** das 333
transações tinham `kind = 'TRANSFER'`, enquanto a Pluggy apontava ~114 como
transferência. Todas contavam como gasto.

---

## Fase 4 — Frontend · 🛠️ implementada

### Três telas

Conexão de contas (widget Pluggy Connect **e** colar `itemId`) · dashboard com
filtros por período, categoria e conta · tela de revisão de categorização.

✅ **A tela de revisão é a mais importante das três**, e não por UX: é ela que
produz `category_source = 'MANUAL'`, que é simultaneamente a correção do usuário,
a base de regras da pipeline híbrida e a única régua para medir o acerto do LLM.
Atrito nessa tela custa o dado que as Fases 3 e 5 dependem. Ela também precisa
permitir ajustar o `kind`, não só a categoria.

🛠️ **Construída primeiro, e por essa razão.** `/revisao` lista a fila de
`NEEDS_REVIEW`; `PATCH /transactions/{id}` grava. O `kind` é editável ao lado da
categoria, com o valor de `categories.kind` como default.

🛠️ **O widget Pluggy Connect ficou de fora**, e o cadastro segue colando o
`itemId`. Exigiria um endpoint de connect token e o script da Pluggy no bundle,
para um único titular cuja conexão já existe.

🛠️ **A correção também acontece em `/transacoes`, e isso o plano não previa.** A
fila só contém o que `decide.decide` marcou; um erro do LLM com confiança 0,95 e
concordância com a Pluggy passa direto e **nunca** é perguntado. Sem um caminho de
correção no extrato, esse erro ficaria sem conserto — e ele é justamente o caso que
mais interessa medir, porque é onde a confiança alta mente.

### 🛠️ A correção destrói o dado que ela produz — e isso precisou de tabela nova

O plano tratava a gravação de `MANUAL` como um `UPDATE` e parou aí. Só que o UPDATE
sobrescreve `category_id` e `category_confidence`: **depois dele não dá para saber
se o titular confirmou a escolha do LLM ou a corrigiu**, que é exatamente a
distinção de que a calibração depende. A régua se apagaria ao ser usada.

Daí `categorization_reviews` (migration `0005`): uma linha append-only por
correção, com a categoria, o `kind`, a origem, o status e a confiança **anteriores**
ao lado da resposta do titular. A medição vira uma consulta:

```sql
SELECT previous_confidence, previous_category_id = new_category_id AS acertou
FROM categorization_reviews;
```

Três decisões que andam junto com ela:

- **`transactions.category_confidence` é zerada na correção.** O número cru passa a
  morar na linha de revisão; mantê-lo faria a coluna significar "quão certo o LLM
  estava" em linha MANUAL e "quão certo ele está" nas demais. A leitura da
  calibração é `categorization_reviews`, e só ela.
- **Sem `UPDATE` no GRANT** do `app_user`. Append-only por privilégio, não por
  convenção. `DELETE` fica porque a eliminação do titular é `DELETE FROM tenants`
  em cascata.
- **Confirmar grava tanto quanto corrigir.** Registrar só a discordância daria uma
  taxa de erro com numerador sem denominador.

### 🛠️ `GET /categories` reusa o catálogo do LLM

O seletor da tela sai de `load_catalog` — o mesmo carregamento que monta o `enum` do
JSON Schema enviado ao Ollama. Não é economia de código: é o que garante que o
humano escolha da mesma lista que o modelo pôde escolher. Duas listas montadas por
caminhos diferentes divergiriam na primeira categoria criada, e a correção deixaria
de ser comparável com a decisão que ela corrige.

### ⚠️ A regra de revisão não é recomputada no cliente

A tela mostra a confiança e o palpite da Pluggy como **fatos crus**, lado a lado, e
não o motivo calculado. Reimplementar `decide.decide` no frontend criaria uma
segunda cópia da regra que diverge da primeira sem produzir erro nenhum — e os dois
números já dizem a quem responde o que ele precisa saber.

O custo é real e vale anotar: quando a regra mudar, a tela não muda junto, e alguém
pode ler "confiança 0,95" numa linha que veio para cá por discordância e achar que a
fila está errada.

### O dashboard soma por `kind` e agrupa por categoria

✅ Não some `amount` sem filtrar `kind`. `TRANSFER` fora dos totais de receita e
despesa — é a razão de o campo existir.

🛠️ `GET /dashboard/summary`, com a aritmética separada em
`app/services/dashboard.py` para poder ser testada sem banco — mesma fronteira de
`catalog.build_catalog` × `load_catalog`.

🔬 **E o dashboard revelou o número que faltava para entender a Fase 3.** Dos
R$ 33.441 de receita, **R$ 33.206 — 99,3% — estão em `NEEDS_REVIEW`**. Não é
defeito: são os `Pix recebido`, e a regra 3c do `SYSTEM_PROMPT` manda o modelo
baixar a confiança quando a descrição não diz quem é o remetente. O sistema está
perguntando exatamente onde deveria.

A consequência de produto é que **o total sem contexto seria uma mentira
confortável**. Daí `needs_review_total` viajar dentro de cada bloco de `kind`, e o
cartão dizer "99% aguardando sua confirmação". O plano original previa um aviso
baseado em `PENDING + FAILED`, que são **zero** — ele nunca teria disparado. O eixo
certo é `NEEDS_REVIEW`, e por valor, não por contagem: em número de lançamentos a
receita é 32 de 45, o que soaria bem menos grave do que é.

🔬 **A questão do índice está fechada, e a resposta é "nenhum".** Medido contra as
333 transações reais:

```
HashAggregate  (cost=126.33..126.36 rows=3) (actual time=0.472..0.474 rows=3)
  ->  Seq Scan on transactions  (actual time=0.011..0.378 rows=333)
Planning Time: 3.281 ms
Execution Time: 0.667 ms
```

O planejamento custa **cinco vezes** mais que a execução, e o `by_category` dá o
mesmo (0,679 ms). Com um filtro de período que pega a tabela quase inteira, o
planner não usaria um índice nem se ele existisse. Índice custa escrita em todo
sync; este não pagaria nada.

Critério para revisitar, registrado em vez de esquecido: `transactions` acima de
~50 mil linhas, **ou** `Execution Time` da agregação acima de 50 ms.

🛠️ **O dashboard é a home**, e o painel de diagnóstico da Fase 0 foi para
`/diagnostico`. Ele continua útil — é onde se vê se o Ollama caiu —, mas uma home
que se declarava provisória enquanto existia um dashboard era um item a mais no
menu sem razão.

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

## Fase 5 — LGPD e segurança · ⚠️ não implementada

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
| ~~Sinal invertido em conta de crédito por alguns connectors~~ | ✅ resolvido na Fase 2: a normalização decide pelo `type`, não pelo sinal |
| ~~Tier gratuito permite disparar atualização de item?~~ | ✅ respondido na Fase 2: não (`REQUEST_REFRESH_BY_DEFAULT = False`) |
| ~~`qwen3.5:9b` respeita o `enum` do schema na prática?~~ | ✅ sim, 10/10 na primeira rodada real |
| ~~Não existe categoria para compra de investimento~~ | ✅ migration `0003`: raiz `Investimentos` com `kind = TRANSFER`, cinco classes de ativo |
| **A fronteira principal × rendimento depende do prompt** | regra 4 do `SYSTEM_PROMPT`; mexer nela sem conferir "Valor recebido de Investimentos" transforma receita em transferência |
| **"Pension"/"Retirement" e "Automatic investment" ficaram sem de/para** | duas categorias da Pluggy disputando uma nossa; `pluggy_category_id` é coluna única |
| ~~`Pix recebido` é `TRANSFER` e engole receita~~ | ✅ migration `0004`: movido para `Receitas` (`INCOME`); a receita reconhecida foi de <1% para ~100% do que entrou |
| **`Pix enviado` continua `TRANSFER`** | espelho não resolvido: pagar alguém por serviço é despesa. A regra 3b manda preferir a categoria do que foi pago, mas o resto fica fora dos gastos |
| **O laço de aprendizado não existe** | o LLM não vê as correções `MANUAL`; realimentar é a pipeline híbrida. A tela já existe, e `categorization_reviews` é o insumo — falta a camada que lê dali |
| **Limiar `LOW_CONFIDENCE = 0.70` continua sem calibração** | ele *dispara* (70 dos 137 `NEEDS_REVIEW`), e agora **há como medir**: `categorization_reviews` guarda `previous_confidence` ao lado da resposta. Falta volume de correções |
| ~~O dashboard não existe~~ | ✅ implementado; `TRANSFER` fora dos totais e a fatia sob revisão exposta por `kind` |
| ~~Índice com `kind` vale a pena?~~ | ✅ medido: Seq Scan, 0,67 ms contra 3,3 ms de planejamento. Nenhum índice novo; revisitar acima de ~50 mil linhas |
| **99,3% da receita nasce em `NEEDS_REVIEW`** | é o desenho funcionando (regra 3c), mas significa que o total de receita depende quase inteiramente da fila ser respondida |
| Descrição corrigida na origem não invalida a categoria já gravada | escape é `POST /categorization/reset`, não invalidação automática |
| O lock de categorização é por processo, como o do sync | ambos deixam de valer com `uvicorn --workers > 1` |
