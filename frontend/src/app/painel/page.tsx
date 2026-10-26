// O dashboard: quanto entrou, quanto saiu e para onde foi, mês a mês.
//
// **Nenhum total é calculado aqui.** Tudo sai de `GET /dashboard/summary`, que
// soma por `kind` e deixa `TRANSFER` de fora — pagamento de fatura e Pix entre
// contas próprias não são gasto. O cliente só formata, e a única conta que faz é a
// proporção de cada categoria para desenhar a barra.
//
// **O número provisório é declarado, não escondido.** Linha que ainda espera o LLM
// ou a revisão entra no total com o `kind` que tem — tirá-la esconderia gasto real
// —, e a tela diz quanto do total ela representa. É o que impede um "gastei R$ X"
// de parecer certo quando metade dele é palpite.

import Link from "next/link";

import { Nav } from "@/components/nav";
import {
  type Account,
  type DashboardSummary,
  formatDate,
  formatMoney,
  type Kind,
  type KindTotal,
  type Money,
  type RootTotal,
  serverFetch,
} from "@/lib/api";

export const dynamic = "force-dynamic";

// O container roda em UTC. Sem o fuso explícito, nas primeiras horas do dia 1º o
// "mês corrente" já seria o seguinte enquanto no Brasil ainda é o anterior.
const FUSO = "America/Sao_Paulo";

type Periodo = { mes: string; from: string; to: string };

function mesCorrente(): string {
  const partes = new Intl.DateTimeFormat("en-CA", {
    timeZone: FUSO,
    year: "numeric",
    month: "2-digit",
  }).formatToParts(new Date());
  const ano = partes.find((p) => p.type === "year")?.value;
  const mes = partes.find((p) => p.type === "month")?.value;
  return `${ano}-${mes}`;
}

function periodo(param: string | undefined): Periodo {
  const mes = param && /^\d{4}-(0[1-9]|1[0-2])$/.test(param) ? param : mesCorrente();
  const [ano, m] = mes.split("-").map(Number);
  // Dia 0 do mês seguinte é o último deste. Tudo em UTC e só aritmética de
  // calendário: data pura não tem fuso, e `new Date("2026-08-01")` local cairia no
  // dia anterior no Brasil.
  const ultimo = new Date(Date.UTC(ano, m, 0)).getUTCDate();
  return { mes, from: `${mes}-01`, to: `${mes}-${String(ultimo).padStart(2, "0")}` };
}

function deslocar(mes: string, delta: number): string {
  const [ano, m] = mes.split("-").map(Number);
  const alvo = new Date(Date.UTC(ano, m - 1 + delta, 1));
  return `${alvo.getUTCFullYear()}-${String(alvo.getUTCMonth() + 1).padStart(2, "0")}`;
}

/** "agosto de 2026". Minúsculo, como o pt-BR escreve; quem quiser maiúscula no
 *  início da frase usa `capitalizar`. */
function nomeDoMes(mes: string): string {
  const [ano, m] = mes.split("-").map(Number);
  return new Intl.DateTimeFormat("pt-BR", {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(Date.UTC(ano, m - 1, 1)));
}

// Não é `text-transform: capitalize`: ele poria maiúscula em toda palavra, e o
// "de" de "agosto de 2026" viraria "De".
function capitalizar(texto: string): string {
  return texto.charAt(0).toUpperCase() + texto.slice(1);
}

const PERCENT = new Intl.NumberFormat("pt-BR", {
  style: "percent",
  maximumFractionDigits: 1,
});

/** Proporção só para desenhar: `null` quando o total é zero ou o sinal diverge
 *  (uma categoria cujos estornos superam as compras não tem "fatia" do gasto). */
function proporcao(parte: Money, total: Money): number | null {
  const t = Number(total);
  if (t === 0) return null;
  const r = Number(parte) / t;
  return r > 0 ? Math.min(r, 1) : null;
}

function classeDoValor(valor: Money): string {
  return valor.startsWith("-") ? "amount-negative" : "amount-positive";
}

function painelHref(mes: string, accountId?: string) {
  const q = new URLSearchParams({ mes });
  if (accountId) q.set("account_id", accountId);
  return `/painel?${q}`;
}

export default async function PainelPage({
  searchParams,
}: {
  searchParams: Promise<{ mes?: string; account_id?: string }>;
}) {
  const params = await searchParams;
  const p = periodo(params.mes);
  const accountId = params.account_id;

  const query = new URLSearchParams({ date_from: p.from, date_to: p.to });
  if (accountId) query.set("account_id", accountId);

  const [summary, accounts] = await Promise.all([
    serverFetch<DashboardSummary>(`/dashboard/summary?${query}`),
    serverFetch<Account[]>("/accounts"),
  ]);

  if (!summary.ok) {
    return (
      <main>
        <Nav />
        <h1>Painel</h1>
        <p className="status-fail">Backend inacessível — {summary.error}</p>
      </main>
    );
  }

  const s = summary.data;
  const { pending, needs_review, failed } = s.undecided;
  const semDecisao = pending + needs_review + failed;
  const vazio =
    s.income.count + s.expense.count + s.transfer.count + s.other_currency_count === 0;

  return (
    <main className="wide">
      <Nav />
      <h1>Painel</h1>

      <p className="row">
        <Link className="chip" href={painelHref(deslocar(p.mes, -1), accountId)}>
          ←
        </Link>
        <strong className="month">{capitalizar(nomeDoMes(p.mes))}</strong>
        <Link className="chip" href={painelHref(deslocar(p.mes, 1), accountId)}>
          →
        </Link>
      </p>

      {accounts.ok && accounts.data.length > 1 && (
        <p className="row">
          <Link className={"chip" + (accountId ? "" : " chip-active")} href={painelHref(p.mes)}>
            todas as contas
          </Link>
          {accounts.data.map((conta) => (
            <Link
              className={"chip" + (conta.id === accountId ? " chip-active" : "")}
              key={conta.id}
              href={painelHref(p.mes, conta.id)}
            >
              {conta.type === "CREDIT" ? "cartão" : "conta"} · {conta.name.slice(0, 22)}
            </Link>
          ))}
        </p>
      )}

      {vazio ? (
        <SemLancamentos mes={p.mes} latest={s.latest_date} accountId={accountId} />
      ) : (
        <>
          <div className="cards">
            <Total titulo="Receitas" valor={s.income} />
            <Total titulo="Despesas" valor={s.expense} />
            <div className="card">
              <h2>Resultado</h2>
              <div className={"kpi " + classeDoValor(s.net)}>{formatMoney(s.net)}</div>
              <p className="muted">receitas menos despesas · transferências fora</p>
            </div>
          </div>

          {semDecisao > 0 && (
            <p className="notice">
              {semDecisao} lançamento(s) deste período ainda sem decisão
              {needs_review > 0 && (
                <>
                  {" "}
                  · {needs_review} aguardando você na <Link href="/revisao">revisão</Link>
                </>
              )}
              {pending > 0 && <> · {pending} na fila do LLM</>}
              {failed > 0 && <> · {failed} com falha</>}. Eles já estão nos totais, com o
              valor e a natureza provisórios — até a categorização, uma transferência
              conta como gasto.
            </p>
          )}

          {s.other_currency_count > 0 && (
            <p className="notice">
              {s.other_currency_count} lançamento(s) em moeda diferente de{" "}
              {s.currency_code} ficaram fora dos totais.
            </p>
          )}

          <Quebra
            titulo="Para onde foi"
            kind="EXPENSE"
            raizes={s.expense_by_category}
            total={s.expense.total}
            periodo={p}
            accountId={accountId}
          />
          <Quebra
            titulo="De onde veio"
            kind="INCOME"
            raizes={s.income_by_category}
            total={s.income.total}
            periodo={p}
            accountId={accountId}
          />

          {s.transfer.count > 0 && (
            <section>
              <h2 className="section">Fora dos totais</h2>
              <p className="muted">
                {s.transfer.count} transferência(s) — dinheiro que só mudou de lugar:
                pagamento de fatura, Pix entre contas próprias, aplicação e resgate.
                Entrou <span className="amount-positive">{formatMoney(s.transfer.inflow)}</span>
                , saiu <span className="amount-negative">{formatMoney(s.transfer.outflow)}</span>.
              </p>
              <details>
                <summary className="muted">ver por categoria</summary>
                <Quebra
                  kind="TRANSFER"
                  raizes={s.transfer_by_category}
                  total={null}
                  periodo={p}
                  accountId={accountId}
                />
              </details>
            </section>
          )}
        </>
      )}
    </main>
  );
}

function Total({ titulo, valor }: { titulo: string; valor: KindTotal }) {
  return (
    <div className="card">
      <h2>{titulo}</h2>
      <div className={"kpi " + classeDoValor(valor.total)}>{formatMoney(valor.total)}</div>
      <p className="muted">
        {valor.count} lançamento(s)
        {valor.undecided_count > 0 && (
          <>
            <br />
            inclui {formatMoney(valor.undecided_total)} sem decisão ({valor.undecided_count})
          </>
        )}
      </p>
    </div>
  );
}

function Quebra({
  titulo,
  kind,
  raizes,
  total,
  periodo,
  accountId,
}: {
  titulo?: string;
  kind: Kind;
  raizes: RootTotal[];
  // `null` esconde a coluna de proporção: transferência não é fatia de nada.
  total: Money | null;
  periodo: Periodo;
  accountId?: string;
}) {
  if (raizes.length === 0) return null;

  // Leva ao extrato filtrado pela categoria, no mesmo período e `kind`: é o caminho
  // de "por que esse número?" até a linha que se corrige.
  const extratoHref = (categoryId: string) => {
    const q = new URLSearchParams({
      category_id: categoryId,
      date_from: periodo.from,
      date_to: periodo.to,
      kind,
    });
    if (accountId) q.set("account_id", accountId);
    return `/transacoes?${q}`;
  };

  return (
    <section>
      {titulo && <h2 className="section">{titulo}</h2>}
      <div className="table-scroll">
        <table className="table breakdown">
          <tbody>
            {raizes.map((raiz) => {
              const fatia = total === null ? null : proporcao(raiz.total, total);
              // Uma folha só, com o nome da própria raiz: repetir a linha embaixo
              // seria ruído.
              const soARaiz =
                raiz.children.length === 1 && raiz.children[0].label === raiz.label;
              return [
                <tr key={raiz.root_id ?? raiz.label} className="root">
                  <td>
                    {soARaiz && raiz.children[0].category_id ? (
                      <Link href={extratoHref(raiz.children[0].category_id)}>{raiz.label}</Link>
                    ) : (
                      raiz.label
                    )}
                    {raiz.undecided_count > 0 && (
                      <span className="badge">{raiz.undecided_count} sem decisão</span>
                    )}
                  </td>
                  <td className="bar-cell">
                    {fatia !== null && (
                      <div className="bar-track" title={PERCENT.format(fatia)}>
                        <div
                          className={"bar " + (kind === "INCOME" ? "bar-income" : "bar-expense")}
                          style={{ width: `${fatia * 100}%` }}
                        />
                      </div>
                    )}
                  </td>
                  <td className="num muted">{fatia !== null ? PERCENT.format(fatia) : ""}</td>
                  <td className={"num " + classeDoValor(raiz.total)}>
                    {formatMoney(raiz.total)}
                  </td>
                </tr>,
                ...(soARaiz
                  ? []
                  : raiz.children.map((filha) => (
                      <tr key={`${raiz.root_id ?? raiz.label}:${filha.category_id}`} className="child">
                        <td>
                          {filha.category_id ? (
                            <Link href={extratoHref(filha.category_id)}>{filha.name}</Link>
                          ) : (
                            filha.name
                          )}
                          <span className="muted"> · {filha.count}</span>
                        </td>
                        {/* `bar-cell` mesmo vazia: some junto com a da raiz no
                            celular, senão a linha filha fica com uma coluna a mais. */}
                        <td className="bar-cell" />
                        <td />
                        <td className="num muted">{formatMoney(filha.total)}</td>
                      </tr>
                    ))),
              ];
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function SemLancamentos({
  mes,
  latest,
  accountId,
}: {
  mes: string;
  latest: string | null;
  accountId?: string;
}) {
  const mesDoUltimo = latest?.slice(0, 7);
  return (
    <div className="card">
      <p>Nenhum lançamento em {nomeDoMes(mes)}.</p>
      {latest && mesDoUltimo && mesDoUltimo !== mes ? (
        <p className="muted">
          O mais recente é de {formatDate(latest)} —{" "}
          <Link href={painelHref(mesDoUltimo, accountId)}>ver {nomeDoMes(mesDoUltimo)}</Link>.
          A coleta é da Pluggy, a cada ~24h; o que ela ainda não trouxe não está aqui.
        </p>
      ) : (
        !latest && (
          <p className="muted">
            Nada sincronizado ainda. Cadastre uma conexão em{" "}
            <Link href="/conexoes">Conexões</Link>.
          </p>
        )
      )}
    </div>
  );
}
