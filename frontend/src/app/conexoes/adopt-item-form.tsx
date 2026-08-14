"use client";

// Cadastro de conexão colando um `itemId`.
//
// É o caminho principal, e não um atalho: `GET /items` da Pluggy não permite
// listagem (responde 401 sempre), então não há como o backend descobrir sozinho
// quais conexões existem. Quem cria o item em meu.pluggy.ai guarda o id.

import { useRouter } from "next/navigation";
import { useState } from "react";

import { BROWSER_API } from "@/lib/api";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function AdoptItemForm() {
  const router = useRouter();
  const [itemId, setItemId] = useState("");
  const [state, setState] = useState<"idle" | "sending">("idle");
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const value = itemId.trim();

    // Valida antes de mandar: o 422 do backend viria com o formato do Pydantic,
    // que não é o que se quer mostrar para quem colou um número de connector.
    if (!UUID.test(value)) {
      setMessage({
        ok: false,
        text: "O itemId é um UUID (8-4-4-4-12). Um número curto como 200 é o connectorId.",
      });
      return;
    }

    setState("sending");
    setMessage(null);
    try {
      const res = await fetch(`${BROWSER_API}/connections`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ item_id: value }),
      });
      const body = await res.json();

      if (!res.ok) {
        setMessage({ ok: false, text: body.detail ?? `HTTP ${res.status}` });
        return;
      }
      setMessage({
        ok: true,
        text:
          res.status === 201
            ? `Conexão criada (${body.connector_name ?? "—"}). Sincronizando…`
            : "Essa conexão já estava cadastrada; sincronizando de novo.",
      });
      setItemId("");
      // Revalida o Server Component pai para a nova conexão aparecer na lista.
      router.refresh();
    } catch (err) {
      setMessage({
        ok: false,
        text: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setState("idle");
    }
  }

  return (
    <form onSubmit={submit} className="stack">
      <label className="field">
        <span>itemId da conexão</span>
        <input
          value={itemId}
          onChange={(e) => setItemId(e.target.value)}
          placeholder="0a1b2c3d-4e5f-6789-abcd-ef0123456789"
          spellCheck={false}
          autoComplete="off"
        />
      </label>
      <div className="row">
        <button type="submit" disabled={state === "sending"}>
          {state === "sending" ? "Consultando a Pluggy…" : "Adicionar conexão"}
        </button>
        <span className="hint">
          Copie de <code>meu.pluggy.ai</code>. Reenviar o mesmo id não duplica.
        </span>
      </div>
      {message && (
        <p className={message.ok ? "status-ok" : "status-fail"}>{message.text}</p>
      )}
    </form>
  );
}
