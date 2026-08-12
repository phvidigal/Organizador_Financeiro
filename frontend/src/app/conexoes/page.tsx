// Conexões bancárias: cadastrar, ver estado, sincronizar.
//
// Server Component para a leitura — chama o backend pela rede do compose, sem
// passar por CORS. As duas partes interativas (formulário e botão) são client
// components próprios.

import Link from "next/link";

import {
  type Account,
  type BankConnection,
  formatDate,
  formatMoney,
  serverFetch,
  type SyncStatus,
} from "../lib/api";
import { AdoptItemForm } from "./adopt-item-form";
import { SyncButton } from "./sync-button";

export const dynamic = "force-dynamic";

// A Pluggy reporta mais estados do que o CHECK do banco aceita; estes são os seis
// que existem em `bank_connections.status`.
const STATUS_LABEL: Record<string, { texto: string; ok: boolean }> = {
  UPDATED: { texto: "atualizada", ok: true },
  UPDATING: { texto: "atualizando na Pluggy", ok: true },
  WAITING_USER_INPUT: { texto: "aguardando você (2º fator?)", ok: false },
  LOGIN_ERROR: { texto: "credenciais recusadas — reconecte", ok: false },
  OUTDATED: { texto: "desatualizada", ok: false },
  ERROR: { texto: "erro", ok: false },
};

export default async function ConexoesPage() {
  const [connections, accounts] = await Promise.all([
    serverFetch<BankConnection[]>("/connections"),
    serverFetch<Account[]>("/accounts"),
  ]);

  if (!connections.ok) {
    return (
      <main>
        <Nav />
        <h1>Conexões</h1>
        <p className="status-fail">Backend inacessível — {connections.error}</p>
      </main>
    );
  }

  const contasPorConexao = new Map<string, Account[]>();
  if (accounts.ok) {
    for (const conta of accounts.data) {
      if (!conta.bank_connection_id) continue;
      const lista = contasPorConexao.get(conta.bank_connection_id) ?? [];
      lista.push(conta);
      contasPorConexao.set(conta.bank_connection_id, lista);
    }
  }

  // O estado "sincronizando agora" vive na memória do backend, então precisa de
  // uma chamada por conexão — a lista é curta e isso evita inventar um endpoint
  // agregador que só esta tela usaria.
  const statuses = await Promise.all(
    connections.data.map((c) => serverFetch<SyncStatus>(`/connections/${c.id}/sync`)),
  );

  return (
    <main>
      <Nav />
      <h1>Conexões</h1>
      <p className="subtitle">
        Contas conectadas via Open Finance. A Pluggy coleta os dados sozinha; aqui
        você lê o que ela já coletou.
      </p>

      <div className="card">
        <h2>Adicionar conexão</h2>
        <AdoptItemForm />
      </div>

      {connections.data.length === 0 && (
        <p className="hint">
          Nenhuma conexão ainda. Conecte seu banco em <code>meu.pluggy.ai</code> e
          cole o <code>itemId</code> acima.
        </p>
      )}

      {connections.data.map((conexao, i) => {
        const status = STATUS_LABEL[conexao.status] ?? {
          texto: conexao.status,
          ok: false,
        };
        const sync = statuses[i];
        const contas = contasPorConexao.get(conexao.id) ?? [];

        return (
          <div className="card" key={conexao.id}>
            <h2>{conexao.connector_name ?? "Conexão"}</h2>

            <p>
              <span className={status.ok ? "status-ok" : "status-fail"}>
                {status.texto}
              </span>
              {conexao.execution_status && (
                <> · <code>{conexao.execution_status}</code></>
              )}
            </p>

            <dl className="meta">
              <dt>Última leitura</dt>
              <dd>{formatDate(conexao.last_success_at)}</dd>
              <dt>Próxima coleta da Pluggy</dt>
              <dd>{formatDate(conexao.next_auto_sync_at)}</dd>
              {conexao.consent_expires_at && (
                <>
                  <dt>Consentimento expira</dt>
                  <dd>{formatDate(conexao.consent_expires_at)}</dd>
                </>
              )}
            </dl>

            {conexao.error && (
              <p className="status-fail">
                {String(conexao.error.message ?? JSON.stringify(conexao.error))}
              </p>
            )}

            {contas.length > 0 && (
              <table className="table">
                <thead>
                  <tr>
                    <th>Conta</th>
                    <th>Tipo</th>
                    <th className="num">Saldo</th>
                  </tr>
                </thead>
                <tbody>
                  {contas.map((conta) => (
                    <tr key={conta.id}>
                      <td>
                        {conta.name}
                        {conta.number && <> · <code>{conta.number}</code></>}
                      </td>
                      <td>{conta.type === "CREDIT" ? "cartão" : "conta"}</td>
                      <td className="num">{formatMoney(conta.balance)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            <SyncButton
              connectionId={conexao.id}
              initiallyRunning={sync.ok ? sync.data.running : false}
            />
          </div>
        );
      })}
    </main>
  );
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
