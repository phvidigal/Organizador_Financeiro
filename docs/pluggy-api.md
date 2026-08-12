# Referência da API Pluggy

Notas levantadas na documentação oficial durante o planejamento da Fase 2, para
que a implementação não precise trabalhar de memória — modelos de linguagem
inventam forma de endpoint com facilidade, e um `GET` errado só aparece em runtime.

**Convenção deste documento:**

- ✅ **verificado** na documentação oficial (link em cada seção);
- 🔬 **medido** contra a API real, com credenciais do tier pessoal (11/08/2026);
- ⚠️ **a confirmar** — não foi lido na fonte; trate como palpite e verifique antes
  de codificar em cima.

Índice completo e legível por máquina: <https://docs.pluggy.ai/llms.txt>

---

## Autenticação

🔬 **Medido contra a API real.**

O fluxo é em duas etapas: credenciais de aplicação (`clientId` + `clientSecret`,
do Dashboard) são trocadas por uma API key de vida curta, enviada depois no header
`X-API-KEY`.

```
POST https://api.pluggy.ai/auth
{"clientId": "<uuid>", "clientSecret": "<string>"}
```

Resposta `200` com **um único campo**:

```jsonc
{ "apiKey": "<JWT de ~700 caracteres>" }
```

Não há campo de validade na resposta. Mas a chave é um JWT, e o `exp` dele mede
**exatamente 120 minutos** — o que confirma o "~2 horas" da documentação. O cliente
lê essa expiração do próprio token (`_jwt_expiry` em
`app/services/pluggy/client.py`) e renova cinco minutos antes, em vez de confiar
numa constante: se a Pluggy encurtar a validade, o cliente acompanha sozinho.

Credencial recusada responde `{"message", "code", "codeDescription", "errorId"}`.
O corpo do erro pode ecoar o que foi enviado, então o cliente extrai só esses
quatro campos por lista de permissão (`_SAFE_AUTH_ERROR_FIELDS`) — o `clientSecret`
nunca pode sair junto com o diagnóstico.

O `codeDescription` é o que vale a pena preservar, porque distingue causas que se
resolvem em lugares diferentes:

| `codeDescription` | Onde está o problema |
|---|---|
| *(ausente)* / credenciais erradas | `.env` |
| `CLIENT_DISABLED` | a Application está desativada no Dashboard — o `.env` está certo |

🔬 Observado: a mesma credencial respondeu `200` e, cerca de quarenta minutos
depois, `401 CLIENT_DISABLED` sem nenhuma alteração do nosso lado.

O cliente deve cachear a key e renovar sob demanda — pedir uma nova a cada
chamada multiplica a latência do sync por dois.

## Connect token

✅ <https://docs.pluggy.ai/reference/connect-token-create>

```
POST https://api.pluggy.ai/connect_token
Header: X-API-KEY: <api key>
```

```jsonc
{
  "itemId": "uuid",          // obrigatório ao ATUALIZAR um item existente
  "options": {
    "clientUserId": "string",
    "webhookUrl": "string",
    "oauthRedirectUri": "string",
    "avoidDuplicates": true
  }
}
```

Resposta: `{ "accessToken": "<jwt>" }` — é o que o widget Pluggy Connect consome.

Erros: `403` falha de autenticação · `404` item não encontrado · `500`.

> Omitir `itemId` numa atualização é erro, não criação silenciosa de item novo.

Criar connect token exige **apenas** estar autenticado com a API key — não há
requisito de plano documentado neste endpoint.

🔬 **Medido: funciona no tier pessoal.** `POST /connect_token` com corpo vazio
responde `200` com `accessToken` de ~890 caracteres. Isso **contraria o relato de
terceiros** (Actual Budget) de que o fluxo do widget trava após o trial do
Dashboard — ver a seção do tier pessoal no fim deste documento. O caminho do
widget Pluggy Connect está, portanto, viável; ficou fora da Fase 2 por escopo, não
por impedimento.

## Items (conexões bancárias)

✅ <https://docs.pluggy.ai/reference/items-retrieve>

🔬 **Não existe listagem de items.** `GET /items` responde `401 Unauthorized` com
qualquer combinação de parâmetros (`?connectorId=`, `?page=`), mesmo com API key
válida. Items só são recuperáveis **por id**: `GET /items/{id}`.

Consequência prática, e a razão de o `itemId` ser digitado pelo usuário: não há
como o backend descobrir sozinho quais conexões existem. Quem cria o item guarda o
id — seja o widget (que o devolve no callback), seja o usuário copiando do
Dashboard. É por isso que `bank_connections` é a nossa fonte da verdade sobre
quais items existem, e não um cache do que a Pluggy listaria.

Campos: `id`, `connector`, `status`, `executionStatus`, `error`
(`code`/`message`/`providerMessage`/`attributes`), `parameter`, `userAction`,
`webhookUrl`, `createdAt`, `updatedAt`, `lastUpdatedAt`, `statusDetail`,
`nextAutoSyncAt`, `consecutiveFailedLoginAttempts`, `consentExpiresAt`,
`products`, `clientUserId`.

`products`: `ACCOUNTS`, `CREDIT_CARDS`, `TRANSACTIONS`, `PAYMENT_DATA`,
`INVESTMENTS`, `INVESTMENTS_TRANSACTIONS`, `IDENTITY`, `BROKERAGE_NOTE`,
`MOVE_SECURITY`, `LOANS`.

`status` inclui `UPDATED`; os demais valores da enumeração não estão detalhados no
schema — o CHECK em `bank_connections.status` cobre
`UPDATING`, `UPDATED`, `WAITING_USER_INPUT`, `LOGIN_ERROR`, `OUTDATED`, `ERROR`.
⚠️ Se um valor fora dessa lista aparecer em produção, a escrita falha: por isso
`execution_status` foi deixado como texto livre, sem CHECK.

`nextAutoSyncAt` e `consentExpiresAt` são os campos que interessam ao sync sob
demanda: o primeiro diz quando virá dado novo, o segundo avisa que a conexão vai
morrer.

### 🔬 `PATCH /items/{id}` não funciona no tier pessoal

```jsonc
{ "message": "MeuPluggy item cant be updated", "code": 400 }
```

Item do connector MeuPluggy **não é atualizável por API**. Quem dita a frequência
é a Pluggy: ela sincroniza sozinha e anuncia a próxima em `nextAutoSyncAt` —
medido em ~24h de intervalo entre `lastUpdatedAt` e `nextAutoSyncAt`.

Consequência para o produto, e é grande: **o nosso sync lê, não atualiza.** A
interface não pode prometer "buscar agora no banco"; o que ela pode dizer é
"lendo o que a Pluggy já coletou" e "próxima coleta às HH:MM". Por isso
`REQUEST_REFRESH_BY_DEFAULT = False` em `app/services/pluggy/sync.py`, e por isso
`bank_connections.next_auto_sync_at` existe (migration `0002_next_auto_sync_at`).

O parâmetro `request_refresh` continua existindo: um plano pago com connector
direto de instituição provavelmente aceita o PATCH.

Observado também: `consentExpiresAt` vem `null` no MeuPluggy — o agregador não
expõe expiração de consentimento.

## Accounts

✅ <https://docs.pluggy.ai/reference/accounts-list>

Campos: `id`, `type`, `subtype`, `number`, `name`, `marketingName`, `balance`,
`currencyCode`, `itemId`, `taxNumber`, `owner`, `bankData`, `creditData`.

- `type`: `BANK` (conta de depósito) · `CREDIT` (cartão)
- `subtype`: `CHECKING_ACCOUNT` · `SAVINGS_ACCOUNT` · `CREDIT_CARD`

> `taxNumber` (CPF) e `owner` **não são persistidos** — dado pessoal sem uso no
> MVP, e minimização é princípio da LGPD (art. 6º, III). Se virarem necessários,
> entram criptografados na Fase 5.

## Transactions

✅ <https://docs.pluggy.ai/reference/transactions-list-by-cursor>

```
GET https://api.pluggy.ai/v2/transactions?accountId=<uuid>
```

Note o `/v2` — o connect token **não** tem prefixo de versão. Não assuma um
padrão único de base path.

| Parâmetro | Uso |
|---|---|
| `accountId` | **obrigatório** |
| `ids` | UUIDs separados por vírgula, máx. 500 |
| `dateFrom` / `dateTo` | `yyyy-mm-dd` |
| `createdAtFrom` | `yyyy-mm-ddThh:mm:ss.000Z` |
| `after` | cursor de paginação |

A resposta traz `next` com a query string da próxima página (o cursor codifica
data + id). Até 500 registros por página.

🔬 **A validação de parâmetros é estrita.** Qualquer chave fora desta tabela
derruba a chamada:

```jsonc
{ "message": "property pageSize should not exist", "code": 400 }
```

Não existe controle de tamanho de página, e não se manda nada "por via das
dúvidas" — foi um `pageSize` especulativo que quebrou o primeiro sync real.

Campos: `id`, `description`, `descriptionRaw`, `amount`, `date`, `type`
(`DEBIT`/`CREDIT`), `category`, `categoryId`, `status` (`POSTED`/`PENDING`),
`providerId`, `merchant`, `paymentData`, `accountId`, `currencyCode`, `balance`,
`providerCode`, `creditCardMetadata`, `operationType`, `createdAt`, `updatedAt`.

### Consequências para o nosso sync

- **Transação muda depois de criada** (`PENDING` → `POSTED`, valor corrigido).
  Por isso a gravação é upsert, nunca insert. Ver `app/services/ingestion.py`.
- **`createdAtFrom` é o filtro do sync incremental**, não `dateFrom`: uma
  transação antiga pode ser criada hoje, e filtrar por data do lançamento a
  perderia.
- **Exclusões não vêm no corpo da resposta.** A documentação orienta integrar com
  os webhooks de criado/atualizado/excluído. Como o desenho é sync sob demanda e
  não webhook, ⚠️ decidir na implementação como detectar remoção — provavelmente
  comparando o conjunto de `id` retornado numa janela contra o que está no banco.
- **🔬 O sinal cru é inconsistente entre tipos de conta — e `type` resolve.**
  Medido no mesmo item, com as duas contas:

  | Conta | `amount` cru | `type` | Significado |
  |---|---|---|---|
  | `BANK` | negativo | `DEBIT` | compra no débito |
  | `CREDIT` | **positivo** | `DEBIT` | compra no cartão |

  A conta de depósito já reporta saída como negativo; o cartão reporta a **mesma
  operação como positivo**. Era exatamente a inversão que se temia — mas ela não
  chega ao banco, porque `normalize_amount` decide pelo `type` (`DEBIT` →
  `-abs()`), não pelo sinal recebido. As duas viram saída corretamente.

  Confirmado no dado real: a grande maioria das transações do cartão gravadas
  como saída, e uma minoria como entrada (estornos e o pagamento da fatura). Por
  isso `CREDIT_SIGN = "AS_REPORTED"` continua correto — a constante existe para o
  dia em que aparecer um connector que erre também o `type`.

## Categories

✅ <https://docs.pluggy.ai/reference/categories-list>

Campos: `id`, `description`, `descriptionTranslated` (pt-BR), `parentId`,
`parentDescription`. Hierarquia de dois níveis, igual à nossa tabela
`categories`. Exemplo da doc: `01010000` "Salary/pro-labore" sob `01000000`
"Income".

`categories.pluggy_category_id` está **NULL** em todas as linhas semeadas, de
propósito: o de/para é preenchido na Fase 2 a partir deste endpoint. Chutar os
ids criaria mapeamentos errados que ninguém notaria até conferir uma
categorização na tela.

🔬 **São 130 categorias**, em dois níveis, com `descriptionTranslated` em pt-BR.
O de/para vive em `app/services/pluggy/category_map.py` e cobre 41 das nossas 50.

A relação é **1:1 por limitação do schema** — `pluggy_category_id` é uma coluna
só. Onde a taxonomia deles é mais fina que a nossa (`Online Courses` /
`University` / `School` contra o nosso `Cursos e mensalidades`), a entrada fica de
fora: a Fase 3 vê "sem contraparte" em vez de "discordância", que é o erro menos
ruim — subestimar concordância não inventa acerto.

As nove sem mapeamento e o porquê: `Pix enviado` / `Pix recebido` (a Pluggy não
distingue direção — isso está no sinal do valor), `Cursos e mensalidades` e
`Estacionamento e pedágio` (N:1), e `Condomínio`, `Manutenção e reformas`,
`Reembolsos`, `Outros`, `Serviços financeiros` (sem contraparte na Pluggy).

## Webhooks

✅ Endpoints existem (`webhooks-list`, `-create`, `-retrieve`, `-update`,
`-delete`), mas estão **fora do escopo** da Fase 2: o desenho escolhido é sync sob
demanda, que dispensa endpoint público.

---

## Tier pessoal "Meu Pluggy"

✅ <https://www.pluggy.ai/meu-pluggy>

- Gratuito e **sem prazo de expiração** para uso pessoal; cadastro em
  <https://meu.pluggy.ai>.
- `clientId` / `clientSecret` saem de uma Application criada no Dashboard.
- Restrito a **um CPF**. Atender clientes, conectar vários CPFs ou transformar em
  produto comercial exige plano pago.
- O Dashboard abre com trial de 15 dias, mas isso vale só para recursos de uso
  comercial — o acesso pessoal continua depois, sem custo.
- Suporte de uso pessoal é via Discord.

🔬 **Medido: um único connector disponível.** `GET /connectors` devolve exatamente
um resultado no tier pessoal:

```jsonc
{ "id": 200, "name": "MeuPluggy", "country": "BR", "type": "PERSONAL_BANK",
  "products": ["ACCOUNTS", "TRANSACTIONS", "CREDIT_CARDS", "INVESTMENTS",
               "INVESTMENTS_TRANSACTIONS", "PAYMENT_DATA", "IDENTITY",
               "BROKERAGE_NOTE"] }
```

Ou seja: no tier pessoal a Pluggy não expõe os bancos individualmente. O usuário
conecta as instituições **dentro do `meu.pluggy.ai`**, e a API enxerga isso como um
único connector agregador. Isso explica o relato do Actual Budget sobre a lista de
connectors — não é que ela "trave" após o trial, é que no uso pessoal ela só tem
esse item por desenho.

O que **não** se confirmou é a parte sobre o widget: `POST /connect_token` responde
`200` normalmente (ver a seção de connect token acima).

**Como a Fase 2 lida com isso:** o `itemId` é digitado pelo usuário, copiado do
`meu.pluggy.ai` — e como `GET /items` não permite listagem, não há alternativa
automática. O widget continua viável para depois.

⚠️ **Verificar cedo:** se o tier gratuito permite disparar atualização de um item.
Se não permitir, a frequência de sincronização é ditada pela Pluggy e o "sync ao
abrir o app" vira apenas leitura do que já foi sincronizado do lado deles — o que
muda o que a interface pode prometer ao usuário.
