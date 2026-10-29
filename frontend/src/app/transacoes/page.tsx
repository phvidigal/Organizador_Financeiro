// Extrato completo, com correção de categoria por linha.
//
// Não é o dashboard: aqui não há soma nem gráfico de propósito, porque enquanto
// houver transação na fila do LLM as transferências ainda contam como receita e
// despesa (ver a invariante do `kind` no CLAUDE.md), e um total nesta tela seria um
// número errado com cara de certo.
//
// A edição por linha existe porque a fila de `/revisao` **não** pega tudo: um erro
// do LLM com confiança 0,95 e concordância com a Pluggy passa direto e nunca é
// perguntado. Sem um caminho de correção aqui, ele ficaria sem conserto.
//
// **Transferência entre contas próprias fica oculta por padrão** — o dinheiro não
// saiu de casa, e na leitura do dia a dia é ruído. Um chip a traz de volta, porque
// o extrato também serve para conferir o que o sync gravou.

import Link from "next/link";

import { CategoryPicker } from "@/components/category-picker";
import { Nav } from "@/components/nav";
import {
  type Account,
  accountLabel,
  type Category,
  formatDate,
  formatMoney,
  type Kind,
  KIND_LABEL,
  type Page,
  RULE_APPLIED_PARAM,
  serverFetch,
  type Transaction,
} from "@/lib/api";

export const dynamic = "force-dynamic";

const PAGE_SIZE = 100;

const SOURCE_LABEL: Record<string, string> = {
  PLUGGY: "Pluggy",
  RULE: "regra",
  EMBEDDING: "embedding",
  LLM: "LLM",
  MANUAL: "você",
};

// Filtros que atravessam a paginação e a troca de conta. Período e `kind` chegam
// do painel, que abre o extrato de uma categoria no mesmo recorte do total que a
// pessoa clicou. `proprias=mostrar` desliga a ocultação da conta própria.
const FILTROS = [
  "account_id",
  "category_id",
  "date_from",
  "date_to",
  "kind",
  "q",
  "proprias",
] as const;
type Filtros = Partial<Record<(typeof FILTROS)[number], string>>;

function href(filtros: Filtros, mudancas: Filtros = {}, offset = 0) {
  const q = new URLSearchParams();
  for (const [chave, valor] of Object.entries({ ...filtros, ...mudancas })) {
    if (valor) q.set(chave, valor);
  }
  if (offset > 0) q.set("offset", String(offset));
  const query = q.toString();
  return query ? `/transacoes?${query}` : "/transacoes";
}

export default async function TransacoesPage({
  searchParams,
}: {
  searchParams: Promise<Filtros & { offset?: string; [RULE_APPLIED_PARAM]?: string }>;
}) {
  const params = await searchParams;
  const offset = Math.max(0, Number(params.offset ?? 0) || 0);
  const filtros: Filtros = {};
  for (const chave of FILTROS) {
    if (params[chave]) filtros[chave] = params[chave];
  }

  // Filtrar por uma categoria é pedir para vê-la — inclusive a conta própria, que é
  // para onde o link do painel leva.
  const ocultarProprias = filtros.proprias !== "mostrar" && !filtros.category_id;

  const query = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
  for (const chave of ["account_id", "category_id", "date_from", "date_to", "kind", "q"] as const) {
    if (filtros[chave]) query.set(chave, filtros[chave]);
  }
  if (ocultarProprias) query.set("hide_own_transfers", "true");

  const [page, accounts, categories] = await Promise.all([
    serverFetch<Page<Transaction>>(`/transactions?${query}`),
    serverFetch<Account[]>("/accounts"),
    serverFetch<Category[]>("/categories"),
  ]);

  if (!page.ok) {
    return (
      <main>
        <Nav />
        <h1>Transações</h1>
        <p className="status-fail">Backend inacessível — {page.error}</p>
      </main>
    );
  }

  const contas = new Map(accounts.ok ? accounts.data.map((a) => [a.id, a]) : []);
  const porId = new Map(categories.ok ? categories.data.map((c) => [c.id, c]) : []);

  const { items, total } = page.data;
  const fim = Math.min(offset + PAGE_SIZE, total);
  const temRecorte = Boolean(
    filtros.category_id || filtros.date_from || filtros.date_to || filtros.kind || filtros.q,
  );
  const regraAplicada = params[RULE_APPLIED_PARAM];

  return (
    <main className="wide">
      <Nav />
      <h1>Transações</h1>
      <p className="subtitle">
        {total} lançamentos {temRecorte ? "neste recorte" : "sincronizados"} · exibindo{" "}
        {total === 0 ? 0 : offset + 1}–{fim}
      </p>

      {regraAplicada !== undefined && (
        <p className="notice">
          Regra criada: {regraAplicada} outro(s) lançamento(s) com o mesmo trecho mudaram
          junto. Ela vale também para os próximos — veja em <Link href="/regras">Regras</Link>.
        </p>
      )}

      {/* GET puro: a busca vira parte da URL, como os outros filtros, e funciona sem
          JavaScript. Os demais filtros atravessam por campos ocultos. */}
      <form className="search" method="get" action="/transacoes">
        {FILTROS.filter((chave) => chave !== "q").map(
          (chave) =>
            filtros[chave] && (
              <input key={chave} type="hidden" name={chave} value={filtros[chave]} />
            ),
        )}
        <input
          type="search"
          name="q"
          defaultValue={filtros.q ?? ""}
          placeholder="buscar na descrição"
          aria-label="Buscar na descrição"
          maxLength={100}
        />
        <button type="submit">Buscar</button>
      </form>

      {temRecorte && (
        <p className="row muted">
          {filtros.category_id && (
            <span>{porId.get(filtros.category_id)?.label ?? "categoria inativa"}</span>
          )}
          {filtros.date_from && filtros.date_to && (
            <span>
              {formatDate(filtros.date_from)} a {formatDate(filtros.date_to)}
            </span>
          )}
          {filtros.kind && <span>{KIND_LABEL[filtros.kind as Kind] ?? filtros.kind}</span>}
          {filtros.q && <span>“{filtros.q}”</span>}
          <Link className="chip" href="/transacoes">
            limpar filtros
          </Link>
        </p>
      )}

      <p className="row">
        {accounts.ok && accounts.data.length > 0 && (
          <>
            <Link
              className={"chip" + (filtros.account_id ? "" : " chip-active")}
              href={href(filtros, { account_id: "" })}
            >
              todas
            </Link>
            {accounts.data.map((conta) => (
              <Link
                className={"chip" + (conta.id === filtros.account_id ? " chip-active" : "")}
                key={conta.id}
                href={href(filtros, { account_id: conta.id })}
              >
                {accountLabel(conta)}
              </Link>
            ))}
          </>
        )}
        {!filtros.category_id && (
          <Link
            className={"chip" + (ocultarProprias ? "" : " chip-active")}
            href={href(filtros, { proprias: ocultarProprias ? "mostrar" : "" })}
          >
            {ocultarProprias
              ? "mostrar transferências entre suas contas"
              : "ocultar transferências entre suas contas"}
          </Link>
        )}
      </p>

      {items.length === 0 ? (
        <p className="hint">
          {temRecorte ? (
            "Nada neste recorte."
          ) : (
            <>
              Nada aqui ainda. Cadastre uma conexão em <Link href="/conexoes">Conexões</Link>{" "}
              e sincronize.
            </>
          )}
        </p>
      ) : (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Data</th>
                <th>Descrição</th>
                <th>Conta</th>
                <th>Categoria</th>
                <th>Natureza</th>
                <th className="num">Valor</th>
                <th>Corrigir</th>
              </tr>
            </thead>
            <tbody>
              {items.map((t) => (
                <tr key={t.id}>
                  <td className="nowrap">{formatDate(t.date)}</td>
                  <td>
                    {t.description_raw}
                    {/* Sem `posted_at` = ainda não compensou. */}
                    {t.posted_at === null && <span className="badge">pendente</span>}
                    {t.counterparty_is_holder && (
                      <span className="badge" title="O outro lado é você, pelo CPF ou nome">
                        sua conta
                      </span>
                    )}
                  </td>
                  <td className="muted nowrap">{accountLabel(contas.get(t.account_id))}</td>
                  <td>
                    {t.category_id
                      ? (porId.get(t.category_id)?.label ?? "categoria inativa")
                      : "—"}
                    {t.category_source && (
                      <span className="badge">
                        {SOURCE_LABEL[t.category_source] ?? t.category_source}
                      </span>
                    )}
                  </td>
                  <td className="muted">{KIND_LABEL[t.kind]}</td>
                  <td
                    className={
                      "num " +
                      (t.amount.startsWith("-") ? "amount-negative" : "amount-positive")
                    }
                  >
                    {formatMoney(t.amount)}
                  </td>
                  <td>
                    {categories.ok && (
                      <CategoryPicker
                        transactionId={t.id}
                        categories={categories.data}
                        currentCategoryId={t.category_id}
                        currentKind={t.kind}
                        suggestedPattern={t.suggested_rule_pattern}
                      />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="row">
        {offset > 0 && (
          <Link className="chip" href={href(filtros, {}, Math.max(0, offset - PAGE_SIZE))}>
            ← anteriores
          </Link>
        )}
        {fim < total && (
          <Link className="chip" href={href(filtros, {}, offset + PAGE_SIZE)}>
            próximas →
          </Link>
        )}
      </div>

      <p className="hint">
        Corrigir aqui grava a mesma resposta que a tela de{" "}
        <Link href="/revisao">revisão</Link> — e nenhuma sincronização a sobrescreve.
      </p>
    </main>
  );
}
