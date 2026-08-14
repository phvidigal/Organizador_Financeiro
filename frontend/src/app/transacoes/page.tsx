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

import Link from "next/link";

import { CategoryPicker } from "@/components/category-picker";
import { Nav } from "@/components/nav";
import {
  type Account,
  type Category,
  formatDate,
  formatMoney,
  KIND_LABEL,
  type Page,
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

export default async function TransacoesPage({
  searchParams,
}: {
  searchParams: Promise<{ offset?: string; account_id?: string; category_id?: string }>;
}) {
  const params = await searchParams;
  const offset = Math.max(0, Number(params.offset ?? 0) || 0);
  const accountId = params.account_id;
  const categoryId = params.category_id;

  const query = new URLSearchParams({
    limit: String(PAGE_SIZE),
    offset: String(offset),
  });
  if (accountId) query.set("account_id", accountId);
  if (categoryId) query.set("category_id", categoryId);

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

  const nomeDaConta = new Map(
    accounts.ok ? accounts.data.map((a) => [a.id, a.type === "CREDIT" ? "cartão" : "conta"]) : [],
  );
  const porId = new Map(categories.ok ? categories.data.map((c) => [c.id, c]) : []);

  const { items, total } = page.data;
  const fim = Math.min(offset + PAGE_SIZE, total);

  return (
    <main className="wide">
      <Nav />
      <h1>Transações</h1>
      <p className="subtitle">
        {total} lançamentos sincronizados · exibindo {total === 0 ? 0 : offset + 1}–{fim}
      </p>

      {accounts.ok && accounts.data.length > 0 && (
        <p className="row">
          <Link className="chip" href="/transacoes">
            todas
          </Link>
          {accounts.data.map((conta) => (
            <Link
              className="chip"
              key={conta.id}
              href={`/transacoes?account_id=${conta.id}`}
            >
              {conta.type === "CREDIT" ? "cartão" : "conta"} · {conta.name.slice(0, 22)}
            </Link>
          ))}
        </p>
      )}

      {items.length === 0 ? (
        <p className="hint">
          Nada aqui ainda. Cadastre uma conexão em{" "}
          <Link href="/conexoes">Conexões</Link> e sincronize.
        </p>
      ) : (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Data</th>
                <th>Descrição</th>
                <th>Origem</th>
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
                  </td>
                  <td className="muted">{nomeDaConta.get(t.account_id) ?? "—"}</td>
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
          <Link
            className="chip"
            href={pageHref(Math.max(0, offset - PAGE_SIZE), accountId, categoryId)}
          >
            ← anteriores
          </Link>
        )}
        {fim < total && (
          <Link className="chip" href={pageHref(offset + PAGE_SIZE, accountId, categoryId)}>
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

function pageHref(offset: number, accountId?: string, categoryId?: string) {
  const q = new URLSearchParams({ offset: String(offset) });
  if (accountId) q.set("account_id", accountId);
  if (categoryId) q.set("category_id", categoryId);
  return `/transacoes?${q}`;
}
