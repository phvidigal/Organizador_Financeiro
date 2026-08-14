// Dashboard — a home.
//
// Todo número desta tela vem somado do backend. Somar aqui transformaria o
// `NUMERIC(18,2)` em double no `JSON.parse`, e 0,10 voltaria a não ser
// representável: exatamente o erro que o tipo do banco existe para evitar. O que a
// tela faz com dinheiro é formatar e desenhar barras.
//
// **Transferência não entra no saldo, e isso é visível na tela.** É a coisa mais
// fácil de alguém achar que é bug — daí o bloco próprio e a linha explicando. Sem
// essa separação, aplicar R$ 5.000 num CDB apareceria como R$ 5.000 de gasto, e o
// resgate dos mesmos R$ 5.000 como receita meses depois.

import Link from "next/link";

import { Nav } from "@/components/nav";
import {
  type Account,
  type DashboardSummary,
  formatMoney,
  KIND_LABEL,
  type KindTotal,
  type Money,
  serverFetch,
  share,
} from "@/lib/api";

export const dynamic = "force-dynamic";

// Rótulo → meses cheios, contando o corrente. Os mesmos nomes vão para a URL.
const PERIODOS: Record<string, { label: string; meses: number }> = {
  "12m": { label: "12 meses", meses: 12 },
  "3m": { label: "3 meses", meses: 3 },
  mes: { label: "mês atual", meses: 1 },
};

const PADRAO = "12m";

/** Primeiro dia do mês `meses - 1` atrás. Espelha `default_period` do backend. */
function inicioDoPeriodo(hoje: Date, meses: number): string {
  const index = hoje.getFullYear() * 12 + hoje.getMonth() - (meses - 1);
  const ano = Math.floor(index / 12);
  const mes = String((index % 12) + 1).padStart(2, "0");
  return `${ano}-${mes}-01`;
}

function isoHoje(hoje: Date): string {
  return `${hoje.getFullYear()}-${String(hoje.getMonth() + 1).padStart(2, "0")}-${String(
    hoje.getDate(),
  ).padStart(2, "0")}`;
}

export default async function DashboardPage({
  searchParams,
}: {
  searchParams: Promise<{ periodo?: string; account_id?: string }>;
}) {
  const params = await searchParams;
  const periodo = params.periodo && params.periodo in PERIODOS ? params.periodo : PADRAO;
  const accountId = params.account_id;

  const hoje = new Date();
  const dateFrom = inicioDoPeriodo(hoje, PERIODOS[periodo].meses);
  const dateTo = isoHoje(hoje);

  const query = new URLSearchParams({ date_from: dateFrom, date_to: dateTo });
  if (accountId) query.set("account_id", accountId);

  const [resumo, accounts] = await Promise.all([
    serverFetch<DashboardSummary>(`/dashboard/summary?${query}`),
    serverFetch<Account[]>("/accounts"),
  ]);

  if (!resumo.ok) {
    return (
      <main>
        <Nav />
        <h1>Dashboard</h1>
        <p className="status-fail">Backend inacessível — {resumo.error}</p>
      </main>
    );
  }

  const d = resumo.data;
  const filtros = (extra: Record<string, string | undefined>) => {
    const q = new URLSearchParams({ periodo, ...(accountId ? { account_id: accountId } : {}) });
    for (const [k, v] of Object.entries(extra)) if (v) q.set(k, v);
    return q;
  };

  return (
    <main className="wide">
      <Nav />
      <h1>Dashboard</h1>
      <p className="subtitle">
        {PERIODOS[periodo].label} · {d.date_from} a {d.date_to}
      </p>

      {d.queue.needs_review > 0 && (
        <p className="aviso">
          <strong>{d.queue.needs_review}</strong> lançamento(s) deste período
          aguardam sua resposta. Enquanto isso, os totais abaixo usam o palpite do
          modelo. <Link href="/revisao">Responder a fila →</Link>
        </p>
      )}

      <div className="row">
        {Object.entries(PERIODOS).map(([chave, { label }]) => (
          <Link
            className={"chip" + (chave === periodo ? " chip-ativo" : "")}
            key={chave}
            href={`/?${new URLSearchParams({
              periodo: chave,
              ...(accountId ? { account_id: accountId } : {}),
            })}`}
          >
            {label}
          </Link>
        ))}
      </div>

      {accounts.ok && accounts.data.length > 0 && (
        <p className="row">
          <Link className={"chip" + (!accountId ? " chip-ativo" : "")} href={`/?periodo=${periodo}`}>
            todas as contas
          </Link>
          {accounts.data.map((conta) => (
            <Link
              className={"chip" + (accountId === conta.id ? " chip-ativo" : "")}
              key={conta.id}
              href={`/?${new URLSearchParams({ periodo, account_id: conta.id })}`}
            >
              {conta.type === "CREDIT" ? "cartão" : "conta"} · {conta.name.slice(0, 22)}
            </Link>
          ))}
        </p>
      )}

      <div className="cards">
        <Cartao titulo="Receita" bloco={d.income} tom="positivo" />
        <Cartao titulo="Despesa" bloco={d.expense} tom="negativo" />
        {/* "Resultado do período", e nunca "Saldo". Num app de finanças "saldo" é
            o que está na conta, e este número é fluxo: não conta o que saiu como
            transferência, e conta a compra do cartão na data da compra em vez da
            data da fatura. Chamá-lo de saldo faz o titular comparar com o
            aplicativo do banco e concluir, com razão, que o número está errado. */}
        <div className="card">
          <h2>Resultado do período</h2>
          <p className={"valor " + (Number(d.net) < 0 ? "amount-negative" : "amount-positive")}>
            {formatMoney(d.net)}
          </p>
          <p className="hint">
            receita menos despesa, sem as transferências — não é o saldo da conta
          </p>
        </div>
        {d.current_balance !== null && (
          <div className="card">
            <h2>Em conta hoje</h2>
            <p
              className={
                "valor " +
                (Number(d.current_balance) < 0 ? "amount-negative" : "amount-positive")
              }
            >
              {formatMoney(d.current_balance)}
            </p>
            <p className="hint">
              saldo reportado pelo banco, fora do recorte de período
            </p>
          </div>
        )}
      </div>

      <div className="card">
        <h2>Transferências</h2>
        <p className="valor">{formatMoney(d.transfer.total)}</p>
        <p className="hint">
          {d.transfer.count} lançamento(s) — pagamento de fatura, aplicação em
          investimento e Pix entre contas próprias. <strong>Fora do saldo de
          propósito</strong>: esse dinheiro não saiu do seu bolso, só mudou de lugar.
          Contá-lo como gasto o somaria duas vezes, na saída e no resgate.
        </p>
      </div>

      <h2 className="secao">Por categoria</h2>
      {d.by_category.length === 0 ? (
        <p className="hint">Nenhum lançamento no período.</p>
      ) : (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Categoria</th>
                <th>Natureza</th>
                <th></th>
                <th className="num">Total</th>
                <th className="num">Lanç.</th>
              </tr>
            </thead>
            <tbody>
              {d.by_category.map((c) => {
                const maior = Math.abs(Number(d.by_category[0].total)) || 1;
                const largura = (Math.abs(Number(c.total)) / maior) * 100;
                return (
                  <tr key={`${c.category_id ?? "sem"}-${c.kind}`}>
                    <td>
                      {c.category_id ? (
                        <Link href={`/transacoes?${filtros({ category_id: c.category_id })}`}>
                          {c.label}
                        </Link>
                      ) : (
                        c.label
                      )}
                      {c.needs_review_count > 0 && (
                        <span className="badge">{c.needs_review_count} a revisar</span>
                      )}
                    </td>
                    <td className="muted">{KIND_LABEL[c.kind]}</td>
                    <td className="barra-celula">
                      <span
                        className={"barra " + (Number(c.total) < 0 ? "barra-saida" : "barra-entrada")}
                        style={{ width: `${largura}%` }}
                      />
                    </td>
                    <td
                      className={
                        "num " + (Number(c.total) < 0 ? "amount-negative" : "amount-positive")
                      }
                    >
                      {formatMoney(c.total)}
                    </td>
                    <td className="num muted">{c.count}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <h2 className="secao">Mês a mês</h2>
      <SerieMensal meses={d.by_month} />
    </main>
  );
}

function Cartao({
  titulo,
  bloco,
  tom,
}: {
  titulo: string;
  bloco: KindTotal;
  tom: "positivo" | "negativo";
}) {
  const pendente = share(bloco.needs_review_total, bloco.total);
  return (
    <div className="card">
      <h2>{titulo}</h2>
      <p className={"valor " + (tom === "negativo" ? "amount-negative" : "amount-positive")}>
        {formatMoney(bloco.total)}
      </p>
      <p className="hint">
        {bloco.count} lançamento(s)
        {bloco.needs_review_count > 0 && (
          <>
            {" · "}
            {/* "Aguardando confirmação", e não "confirmado": CATEGORIZED por LLM
                significa que o modelo não perguntou, o que não é a mesma coisa que
                você ter respondido. */}
            <Link href="/revisao">{pendente}% aguardando sua confirmação</Link>
          </>
        )}
      </p>
    </div>
  );
}

function SerieMensal({ meses }: { meses: { month: string; income: Money; expense: Money }[] }) {
  if (meses.length === 0) return <p className="hint">Nenhum lançamento no período.</p>;

  // Uma escala só para receita e despesa, senão as duas barras deixam de ser
  // comparáveis entre si e o gráfico vira decoração.
  const escala =
    Math.max(...meses.map((m) => Math.max(Math.abs(Number(m.income)), Math.abs(Number(m.expense))))) ||
    1;

  return (
    <div className="table-scroll">
      <table className="table">
        <thead>
          <tr>
            <th>Mês</th>
            <th></th>
            <th className="num">Receita</th>
            <th className="num">Despesa</th>
          </tr>
        </thead>
        <tbody>
          {meses.map((m) => (
            <tr key={m.month}>
              <td className="nowrap">{m.month}</td>
              <td className="barra-celula">
                <span
                  className="barra barra-entrada"
                  style={{ width: `${(Math.abs(Number(m.income)) / escala) * 100}%` }}
                />
                <span
                  className="barra barra-saida"
                  style={{ width: `${(Math.abs(Number(m.expense)) / escala) * 100}%` }}
                />
              </td>
              <td className="num amount-positive">{formatMoney(m.income)}</td>
              <td className="num amount-negative">{formatMoney(m.expense)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
