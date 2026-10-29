// As regras do titular — o que a revisão aprendeu.
//
// Uma regra nasce de uma resposta, com "sempre assim com [trecho]" na revisão ou no
// extrato, e não aqui: é ali que o trecho foi conferido contra um lançamento de
// verdade. Esta tela só mostra e desfaz.
//
// A ordem de quem decide é fixa, e a tela a lembra: o que você respondeu à mão
// nunca muda; a transferência entre contas suas é reconhecida pelo CPF antes de
// qualquer regra; e só o que nenhuma regra pega vai para o LLM.

import Link from "next/link";

import { Nav } from "@/components/nav";
import { RuleToggle } from "@/components/rule-toggle";
import { formatDate, type Rule, serverFetch } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function RegrasPage() {
  const rules = await serverFetch<Rule[]>("/rules");

  if (!rules.ok) {
    return (
      <main>
        <Nav />
        <h1>Regras</h1>
        <p className="status-fail">Backend inacessível — {rules.error}</p>
      </main>
    );
  }

  const ativas = rules.data.filter((r) => r.is_active).length;

  return (
    <main className="wide">
      <Nav />
      <h1>Regras</h1>
      <p className="subtitle">
        {rules.data.length === 0
          ? "Nenhuma regra ainda."
          : `${ativas} ativa(s) de ${rules.data.length}`}
      </p>

      {rules.data.length === 0 ? (
        <p className="hint">
          Na <Link href="/revisao">revisão</Link>, marque “sempre assim com [trecho]” ao
          responder: o próximo lançamento com o mesmo trecho recebe a mesma categoria,
          sem passar pelo LLM nem voltar para a fila.
        </p>
      ) : (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Quando a descrição contém</th>
                <th>Categoria</th>
                <th className="num">Lançamentos</th>
                <th>Criada</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rules.data.map((r) => (
                <tr key={r.id} className={r.is_active ? undefined : "muted"}>
                  <td>
                    <code>{r.pattern}</code>
                    {!r.is_active && <span className="badge">desativada</span>}
                  </td>
                  <td>
                    {r.category_label ? (
                      <Link href={`/transacoes?category_id=${r.category_id}`}>
                        {r.category_label}
                      </Link>
                    ) : (
                      <span className="status-fail">categoria inativa — a regra não vale</span>
                    )}
                  </td>
                  <td className="num">{r.applied_count}</td>
                  <td className="nowrap muted">{formatDate(r.created_at)}</td>
                  <td>
                    <RuleToggle ruleId={r.id} active={r.is_active} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="hint">
        Quem decide, nesta ordem: o que você respondeu à mão (nada sobrescreve), a
        transferência entre contas suas (reconhecida pelo CPF de quem pagou ou
        recebeu), estas regras e, por último, o LLM. Desativar uma regra devolve à fila
        só os lançamentos que ela categorizou.
      </p>
    </main>
  );
}
