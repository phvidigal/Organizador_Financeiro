// Diagnóstico das três pontas.
//
// Nasceu como a home da Fase 0 e saiu de lá quando o dashboard chegou — mas
// continua valendo o que sempre valeu: prova que o container do frontend alcança o
// backend pela rede do compose, que o backend alcança o Postgres, e que alcança o
// Ollama rodando fora do Docker. É a peça mais frágil do setup e a mais barata de
// verificar aqui.

import Link from "next/link";

import { Nav } from "@/components/nav";

// `INTERNAL_API_URL` e não `NEXT_PUBLIC_API_URL`: este componente roda no servidor,
// dentro da rede do compose, onde `localhost` seria o próprio container do frontend.
const API_URL = process.env.INTERNAL_API_URL ?? "http://backend:8000";

type HealthResponse = { status: string; db: string };
type OllamaResponse = {
  status: string;
  base_url: string;
  expected_model?: string;
  model_available?: boolean;
  error?: string;
};

async function fetchJson<T>(path: string): Promise<T | { error: string }> {
  try {
    // `no-store`: sem isso o Next cacheia a resposta no build e o painel passa a
    // mostrar um status congelado em vez do estado atual.
    const res = await fetch(`${API_URL}${path}`, { cache: "no-store" });
    if (!res.ok) return { error: `HTTP ${res.status}` };
    return (await res.json()) as T;
  } catch (err) {
    return { error: err instanceof Error ? err.message : String(err) };
  }
}

function StatusLine({ ok, children }: { ok: boolean; children: React.ReactNode }) {
  return <span className={ok ? "status-ok" : "status-fail"}>{children}</span>;
}

export default async function Home() {
  const [health, ollama] = await Promise.all([
    fetchJson<HealthResponse>("/health"),
    fetchJson<OllamaResponse>("/health/ollama"),
  ]);

  const backendOk = !("error" in health);
  const dbOk = backendOk && health.db === "ok";
  const ollamaOk = !("error" in ollama) && ollama.status === "ok";

  return (
    <main>
      <Nav />

      <h1>Diagnóstico</h1>
      <p className="subtitle">
        As três pontas do sistema, do container do frontend ao Ollama no host.
      </p>

      <div className="card">
        <h2>Backend</h2>
        {backendOk ? (
          <StatusLine ok>conectado</StatusLine>
        ) : (
          <StatusLine ok={false}>inacessível — {health.error}</StatusLine>
        )}{" "}
        <code>{API_URL}</code>
      </div>

      <div className="card">
        <h2>PostgreSQL</h2>
        {dbOk ? (
          <StatusLine ok>conectado</StatusLine>
        ) : (
          <StatusLine ok={false}>
            {backendOk ? health.db : "sem resposta do backend"}
          </StatusLine>
        )}
      </div>

      <div className="card">
        <h2>Ollama (host)</h2>
        {"error" in ollama ? (
          <StatusLine ok={false}>sem resposta do backend — {ollama.error}</StatusLine>
        ) : ollamaOk ? (
          <>
            <StatusLine ok>conectado</StatusLine>{" "}
            <code>{ollama.expected_model}</code>{" "}
            {ollama.model_available ? (
              <StatusLine ok>disponível</StatusLine>
            ) : (
              <StatusLine ok={false}>modelo não encontrado</StatusLine>
            )}
          </>
        ) : (
          <StatusLine ok={false}>inacessível em {ollama.base_url}</StatusLine>
        )}
      </div>

      <p className="next-steps">
        O Ollama roda fora do Docker, alcançado em <code>host.docker.internal</code>
        — é a ponta que costuma cair primeiro quando a{" "}
        <Link href="/revisao">fila de revisão</Link> para de andar.
      </p>
    </main>
  );
}
