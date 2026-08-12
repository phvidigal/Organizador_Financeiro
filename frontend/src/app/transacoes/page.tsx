// Extrato cru, para conferir o que o sync gravou.
//
// Não é o dashboard — isso é a Fase 4. Aqui não há soma nem gráfico de propósito:
// enquanto as transferências não forem categorizadas, elas contam como receita e
// despesa (ver a invariante do `kind` no CLAUDE.md), e um total nesta tela seria
// um número errado com cara de certo.

import Link from "next/link";

import {
  type Account,
  formatDate,
  formatMoney,
  type Page,
  serverFetch,
  type Transaction,
} from "../lib/api";

export const dynamic = "force-dynamic";

const PAGE_SIZE = 100;

const KIND_LABEL: Record<string, string> = {
  INCOME: "entrada",
  EXPENSE: "saída",
  TRANSFER: "transferência",
};

export default async function TransacoesPage({
  searchParams,
}: {
  searchParams: Promise<{ offset?: string; account_id?: string }>;
}) {
  const params = await searchParams;
  const offset = Math.max(0, Number(params.offset ?? 0) || 0);
  const accountId = params.account_id;

  const query = new URLSearchParams({
    limit: String(PAGE_SIZE),
    offset: String(offset),
  });
  if (accountId) query.set("account_id", accountId);

  const [page, accounts] = await Promise.all([
    serverFetch<Page<Transaction>>(`/transactions?${query}`),
    serverFetch<Account[]>("/accounts"),
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
                <th>Categoria (Pluggy)</th>
                <th>Natureza</th>
                <th className="num">Valor</th>
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
                  <td className="muted">{t.pluggy_category_name ?? "—"}</td>
                  <td className="muted">{KIND_LABEL[t.kind] ?? t.kind}</td>
                  <td
                    className={
                      "num " +
                      (t.amount.startsWith("-") ? "amount-negative" : "amount-positive")
                    }
                  >
                    {formatMoney(t.amount)}
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
            href={pageHref(Math.max(0, offset - PAGE_SIZE), accountId)}
          >
            ← anteriores
          </Link>
        )}
        {fim < total && (
          <Link className="chip" href={pageHref(offset + PAGE_SIZE, accountId)}>
            próximas →
          </Link>
        )}
      </div>

      <p className="hint">
        Categoria é o palpite da Pluggy, guardado mas ainda não adotado — todos os
        lançamentos seguem na fila da categorização por LLM (Fase 3).
      </p>
    </main>
  );
}

function pageHref(offset: number, accountId?: string) {
  const q = new URLSearchParams({ offset: String(offset) });
  if (accountId) q.set("account_id", accountId);
  return `/transacoes?${q}`;
}

function Nav() {
  return (
    <nav className="nav">
      <Link href="/">Diagnóstico</Link>
      <Link href="/conexoes">Conexões</Link>
      <Link href="/transacoes">Transações</Link>
    </nav>
  );
}
