# Referência da API Pluggy

Notas levantadas na documentação oficial durante o planejamento da Fase 2, para
que a implementação não precise trabalhar de memória — modelos de linguagem
inventam forma de endpoint com facilidade, e um `GET` errado só aparece em runtime.

**Convenção deste documento:**

- ✅ **verificado** na documentação oficial (link em cada seção);
- ⚠️ **a confirmar** — não foi lido na fonte; trate como palpite e verifique antes
  de codificar em cima.

Índice completo e legível por máquina: <https://docs.pluggy.ai/llms.txt>

---

## Autenticação

⚠️ **A confirmar** — esta é a única parte não lida na fonte.

O fluxo é em duas etapas: credenciais de aplicação (`clientId` + `clientSecret`,
do Dashboard) são trocadas por uma API key de vida curta, enviada depois no header
`X-API-KEY`. O endpoint de troca é `POST /auth` e a chave dura cerca de 2 horas.

Confirmar shape exato em <https://docs.pluggy.ai/reference/auth-create> antes de
implementar. O uso do header `X-API-KEY` está ✅ verificado (documentado no
endpoint de connect token).

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

## Items (conexões bancárias)

✅ <https://docs.pluggy.ai/reference/items-retrieve>

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
demanda: o primeiro diz se vale a pena forçar atualização, o segundo avisa que a
conexão vai morrer.

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
- **`amount` + `type`** alimentam a normalização de sinal (negativo = saída).
  ⚠️ Alguns connectors invertem a convenção em conta de crédito — conferir com o
  cartão real antes de confiar. `raw_payload` guarda o original.

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

⚠️ **Fonte de terceiros, não confirmada na doc oficial:** a documentação do
Actual Budget (<https://actualbudget.org/docs/advanced/bank-sync/pluggyai/>)
afirma que, no fluxo pessoal, o usuário conecta o banco em `meu.pluggy.ai` e
copia o `itemId` do Dashboard, e que "a lista de connectors deixa de ser editável"
após o trial. Isso conflita com o endpoint de connect token, que não documenta
restrição de plano.

**Como a Fase 2 lida com essa incerteza:** suportar os dois caminhos. Colar um
`itemId` existente funciona com certeza (o usuário já tem um) e destrava o
desenvolvimento imediatamente; o widget é a UX melhor, a validar na prática.

⚠️ **Verificar cedo:** se o tier gratuito permite disparar atualização de um item.
Se não permitir, a frequência de sincronização é ditada pela Pluggy e o "sync ao
abrir o app" vira apenas leitura do que já foi sincronizado do lado deles — o que
muda o que a interface pode prometer ao usuário.
