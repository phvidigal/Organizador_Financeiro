"use client";

// Dispara o sync e acompanha até terminar.
//
// O POST devolve 202 na hora porque ler todas as contas e páginas de transações
// leva de segundos a minutos. Sem o polling, a tela ficaria mostrando o estado
// anterior até um F5 manual.

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { BROWSER_API, type SyncStatus } from "../lib/api";

const POLL_MS = 3000;

type Phase = "idle" | "starting" | "running" | "done";

export function SyncButton({
  connectionId,
  initiallyRunning,
}: {
  connectionId: string;
  initiallyRunning: boolean;
}) {
  const router = useRouter();
  const [phase, setPhase] = useState<Phase>(initiallyRunning ? "running" : "idle");
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (phase !== "running") return;

    timer.current = setInterval(async () => {
      try {
        const res = await fetch(`${BROWSER_API}/connections/${connectionId}/sync`);
        if (!res.ok) return;
        const status: SyncStatus = await res.json();
        if (status.running) return;

        setPhase("done");
        const out = status.last_outcome;
        setMessage(
          out
            ? {
                ok: out.status !== "FAILED",
                text:
                  out.status === "FAILED"
                    ? `Falhou: ${String(out.error?.message ?? "erro desconhecido")}`
                    : `${out.transactions_upserted} lançamento(s) gravado(s) em ` +
                      `${out.accounts_synced} conta(s).`,
              }
            : { ok: true, text: "Sincronização concluída." },
        );
        router.refresh();
      } catch {
        // Falha de rede num tick de polling não é motivo para desistir: o próximo
        // tick tenta de novo, e o estado durável está no banco de qualquer forma.
      }
    }, POLL_MS);

    // Sem o clear, trocar de página deixaria o intervalo rodando para sempre.
    return () => {
      if (timer.current) clearInterval(timer.current);
      timer.current = null;
    };
  }, [phase, connectionId, router]);

  async function start() {
    setPhase("starting");
    setMessage(null);
    try {
      const res = await fetch(`${BROWSER_API}/connections/${connectionId}/sync`, {
        method: "POST",
      });
      const status: SyncStatus = await res.json();

      if (res.status === 409) {
        // Throttle não é erro: é o sistema evitando reler o mesmo conteúdo.
        const minutos = Math.ceil((status.retry_after_seconds ?? 0) / 60);
        setPhase("idle");
        setMessage({
          ok: true,
          text: `Sincronizado há pouco. Tente de novo em ${minutos} min.`,
        });
        return;
      }
      if (!res.ok) {
        setPhase("idle");
        setMessage({ ok: false, text: `HTTP ${res.status}` });
        return;
      }
      setPhase("running");
    } catch (err) {
      setPhase("idle");
      setMessage({ ok: false, text: err instanceof Error ? err.message : String(err) });
    }
  }

  const busy = phase === "starting" || phase === "running";

  return (
    <div className="row">
      <button onClick={start} disabled={busy}>
        {phase === "starting"
          ? "Iniciando…"
          : phase === "running"
            ? "Sincronizando…"
            : "Sincronizar"}
      </button>
      {message && (
        <span className={message.ok ? "status-ok" : "status-fail"}>{message.text}</span>
      )}
    </div>
  );
}
