"use client";

// Desativar e reativar uma regra.
//
// Desativar devolve à fila do LLM o que a regra decidiu — e só isso; o que alguém
// redecidiu depois não volta. A categorização dessas linhas é disparada **depois**
// da resposta, num request à parte: só então a escrita já está confirmada no banco,
// e o job não corre o risco de ler a fila antes dela.

import { useRouter } from "next/navigation";
import { useState } from "react";

import { BROWSER_API } from "@/lib/api";

export function RuleToggle({ ruleId, active }: { ruleId: string; active: boolean }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toggle() {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${BROWSER_API}/rules/${ruleId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_active: !active }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`);
        return;
      }
      const body = (await res.json()) as { transactions_changed: number };
      if (active && body.transactions_changed > 0) {
        // 409 é "já está rodando" — a execução em curso pega as linhas de volta.
        await fetch(`${BROWSER_API}/categorization/run`, { method: "POST" }).catch(() => {});
      }
      router.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="picker">
      <button onClick={toggle} disabled={busy}>
        {busy ? "…" : active ? "Desativar" : "Reativar"}
      </button>
      {error && <span className="status-fail">{error}</span>}
    </span>
  );
}
