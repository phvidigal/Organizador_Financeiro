// A fila de revisão — a tela mais importante da Fase 4.
//
// O que ela produz não é conforto de uso: é `category_source = 'MANUAL'`, que é ao
// mesmo tempo a correção do titular, a base da futura pipeline de regras e a única
// régua para medir o acerto do LLM. Hoje há zero correções no banco, e por isso o
// limiar `LOW_CONFIDENCE = 0.70` continua sem calibração.
//
// **Os motivos de revisão aparecem como fatos crus, não como veredito.** A regra
// que manda uma linha para cá vive em `decide.decide`, no backend: confiança abaixo
// do limiar, discordância de raiz com a Pluggy, ou uma escolha que **é** uma
// pergunta ("Pix enviado", conta própria sem evidência). Reimplementá-la aqui
// criaria uma segunda cópia que diverge da primeira sem ninguém notar — então a
// pergunta vem pronta do backend (`review_question`), e o resto são os números.

import Link from "next/link";

import { CategoryPicker } from "@/components/category-picker";
import { Nav } from "@/components/nav";
import {
  type Account,
  accountLabel,
  type Category,
  type CategorizationStatusRead,
  formatConfidence,
  formatDate,
  formatMoney,
  KIND_LABEL,
  type Page,
  RULE_APPLIED_PARAM,
  serverFetch,
  type Transaction,
} from "@/lib/api";

export const dynamic = "force-dynamic";

const PAGE_SIZE = 50;

export default async function RevisaoPage({
  searchParams,
}: {
  searchParams: Promise<{ offset?: string; [RULE_APPLIED_PARAM]?: string }>;
}) {
  const params = await searchParams;
  const offset = Math.max(0, Number(params.offset ?? 0) || 0);
  const regraAplicada = params[RULE_APPLIED_PARAM];

  const query = new URLSearchParams({
    categorization_status: "NEEDS_REVIEW",
    limit: String(PAGE_SIZE),
    offset: String(offset),
  });

  const [page, categories, accounts, status] = await Promise.all([
    serverFetch<Page<Transaction>>(`/transactions?${query}`),
    serverFetch<Category[]>("/categories"),
    serverFetch<Account[]>("/accounts"),
    serverFetch<CategorizationStatusRead>("/categorization/status"),
  ]);

  if (!page.ok || !categories.ok) {
    // Sem a taxonomia não há o que oferecer, e sem a fila não há o que responder:
    // as duas são bloqueantes, ao contrário de contas e contadores.
    const erro = !page.ok ? page.error : !categories.ok ? categories.error : "";
    return (
      <main>
        <Nav />
        <h1>Revisão</h1>
        <p className="status-fail">Backend inacessível — {erro}</p>
      </main>
    );
  }

  const porId = new Map(categories.data.map((c) => [c.id, c]));
  const contas = new Map(accounts.ok ? accounts.data.map((a) => [a.id, a]) : []);

  const { items, total } = page.data;
  const fim = Math.min(offset + PAGE_SIZE, total);
  const fila = status.ok ? status.data.queue : null;

  return (
    <main className="wide">
      <Nav />
      <h1>Revisão</h1>
      <p className="subtitle">
        {total === 0
          ? "Nada aguardando resposta."
          : `${total} lançamento(s) na fila · exibindo ${offset + 1}–${fim}`}
        {fila && (
          <>
            {" "}
            · {fila.categorized} já decidido(s), {fila.pending} na fila do LLM
            {fila.failed > 0 && `, ${fila.failed} com falha`}
          </>
        )}
      </p>

      {regraAplicada !== undefined && (
        <p className="notice">
          Regra criada: {regraAplicada} outro(s) lançamento(s) com o mesmo trecho foram
          categorizados junto, e os próximos não vão mais aparecer aqui. Veja em{" "}
          <Link href="/regras">Regras</Link>.
        </p>
      )}

      {items.length === 0 ? (
        <p className="hint">
          A fila está vazia. Novos lançamentos aparecem aqui depois da próxima{" "}
          <Link href="/conexoes">sincronização</Link> e da categorização.
        </p>
      ) : (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Data</th>
                <th>Descrição</th>
                <th className="num">Valor</th>
                <th>Sugestão do LLM</th>
                <th>Por que revisar</th>
                <th>Resposta</th>
              </tr>
            </thead>
            <tbody>
              {items.map((t) => {
                const sugerida = t.category_id ? porId.get(t.category_id) : undefined;
                return (
                  <tr key={t.id}>
                    <td className="nowrap">{formatDate(t.date)}</td>
                    <td>
                      {t.description_raw}
                      <span className="muted"> · {accountLabel(contas.get(t.account_id))}</span>
                    </td>
                    <td
                      className={
                        "num " +
                        (t.amount.startsWith("-") ? "amount-negative" : "amount-positive")
                      }
                    >
                      {formatMoney(t.amount)}
                    </td>
                    <td>
                      {/* Categoria desativada depois de gravada não resolve rótulo:
                          `GET /categories` só devolve as ativas, de propósito. */}
                      {t.category_id
                        ? (sugerida?.label ?? "categoria inativa")
                        : "— nenhuma —"}
                      <br />
                      <span className="muted">{KIND_LABEL[t.kind]}</span>
                    </td>
                    <td className="muted">
                      {t.review_question && (
                        <span className="question">{t.review_question}</span>
                      )}
                      confiança {formatConfidence(t.category_confidence)}
                      <br />
                      Pluggy: {t.pluggy_category_name ?? "—"}
                    </td>
                    <td>
                      <CategoryPicker
                        transactionId={t.id}
                        categories={categories.data}
                        currentCategoryId={t.category_id}
                        currentKind={t.kind}
                        suggestedPattern={t.suggested_rule_pattern}
                        offerConfirm
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <div className="row">
        {offset > 0 && (
          <Link className="chip" href={`/revisao?offset=${Math.max(0, offset - PAGE_SIZE)}`}>
            ← anteriores
          </Link>
        )}
        {fim < total && (
          <Link className="chip" href={`/revisao?offset=${offset + PAGE_SIZE}`}>
            próximas →
          </Link>
        )}
      </div>

      <p className="hint">
        Confirmar uma sugestão conta tanto quanto corrigi-la: as duas viram resposta
        do titular, e é a comparação entre elas que diz se a confiança do modelo
        significa alguma coisa. Marque “sempre assim” quando o mesmo trecho for se
        repetir — o próximo lançamento com ele não passa mais por aqui.
      </p>
    </main>
  );
}
