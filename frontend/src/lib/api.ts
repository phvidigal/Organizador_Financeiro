// Cliente da API e tipos espelhando os schemas do backend.
//
// Duas URLs, de propósito:
//
// * `INTERNAL_API_URL` (http://backend:8000) — Server Components, dentro da rede
//   do compose. Não passa por CORS e não expõe nada ao bundle.
// * `NEXT_PUBLIC_API_URL` (http://localhost:8000) — o que o navegador alcança.
//   Vai para o bundle, então nunca pode carregar segredo.

const SERVER_API = process.env.INTERNAL_API_URL ?? "http://backend:8000";

export const BROWSER_API =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// Dinheiro chega como **string**, sempre com duas casas. `JSON.parse` do
// navegador transformaria número em double, e 0,10 voltaria a não ser
// representável — exatamente o que o NUMERIC do banco existe para evitar.
export type Money = string;

export type BankConnection = {
  id: string;
  pluggy_item_id: string;
  connector_id: number | null;
  connector_name: string | null;
  status: string;
  execution_status: string | null;
  products: string[] | null;
  last_synced_at: string | null;
  last_success_at: string | null;
  next_auto_sync_at: string | null;
  consent_expires_at: string | null;
  error: Record<string, unknown> | null;
};

export type CategoryMapReport = {
  mapped: number;
  already_mapped: number;
  unmatched_pluggy: string[];
  unmapped_local: string[];
  conflicts: string[];
};

export type SyncOutcome = {
  status: string;
  started_at: string;
  finished_at: string | null;
  accounts_synced: number;
  transactions_upserted: number;
  item_refresh_supported: boolean | null;
  warnings: string[];
  error: Record<string, unknown> | null;
  categories: CategoryMapReport | null;
};

export type SyncStatus = {
  connection_id: string;
  running: boolean;
  status: string;
  execution_status: string | null;
  last_synced_at: string | null;
  last_success_at: string | null;
  next_auto_sync_at: string | null;
  consent_expires_at: string | null;
  error: Record<string, unknown> | null;
  throttled: boolean;
  retry_after_seconds: number | null;
  last_outcome: SyncOutcome | null;
};

export type Account = {
  id: string;
  bank_connection_id: string | null;
  pluggy_account_id: string | null;
  type: string;
  subtype: string | null;
  name: string;
  marketing_name: string | null;
  number: string | null;
  balance: Money | null;
  currency_code: string;
};

export type Transaction = {
  id: string;
  account_id: string;
  source: string;
  external_id: string | null;
  amount: Money;
  currency_code: string;
  kind: Kind;
  date: string;
  posted_at: string | null;
  description_raw: string;
  description_clean: string | null;
  category_id: string | null;
  categorization_status: CategorizationStatus;
  category_source: CategorySource | null;
  // A confiança **crua** do modelo, entre 0 e 1 — string, como todo NUMERIC.
  // É NULL depois de uma correção manual: o número passa a morar em
  // `categorization_reviews`, no backend.
  category_confidence: Money | null;
  pluggy_category_id: string | null;
  pluggy_category_name: string | null;
};

export type CategorizationStatus =
  | "PENDING"
  | "CATEGORIZED"
  | "NEEDS_REVIEW"
  | "FAILED";

export type CategorySource = "PLUGGY" | "RULE" | "EMBEDDING" | "LLM" | "MANUAL";

export type Kind = "INCOME" | "EXPENSE" | "TRANSFER";

// Espelha `CategoryRead` do backend, que sai de `load_catalog` — o mesmo catálogo
// que vira o `enum` do JSON Schema do Ollama. O rótulo é qualificado
// ("Alimentação > Delivery") porque o nome cru pode ser ambíguo entre dois pais.
export type Category = {
  id: string;
  label: string;
  kind: Kind;
  root_id: string;
  pluggy_category_id: string | null;
};

export type QueueCounts = {
  pending: number;
  categorized: number;
  needs_review: number;
  failed: number;
};

export type CategorizationStatusRead = {
  tenant_id: string;
  running: boolean;
  queue: QueueCounts;
};

export type Page<T> = {
  items: T[];
  total: number;
  limit: number;
  offset: number;
};

export type Fetched<T> = { ok: true; data: T } | { ok: false; error: string };

export async function serverFetch<T>(path: string): Promise<Fetched<T>> {
  try {
    // `no-store`: sem isso o Next cacheia no build e a tela mostra um estado
    // congelado em vez do atual.
    const res = await fetch(`${SERVER_API}${path}`, { cache: "no-store" });
    if (!res.ok) {
      const detail = await res.text();
      return { ok: false, error: `HTTP ${res.status} — ${detail.slice(0, 300)}` };
    }
    return { ok: true, data: (await res.json()) as T };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : String(err) };
  }
}

const BRL = new Intl.NumberFormat("pt-BR", {
  style: "currency",
  currency: "BRL",
});

/** Formata para exibição. Nunca somar no cliente: os totais saem do backend. */
export function formatMoney(value: Money | null): string {
  if (value === null) return "—";
  return BRL.format(Number(value));
}

/** Confiança do modelo em pt-BR, com as três casas que o NUMERIC(4,3) carrega. */
export function formatConfidence(value: Money | null): string {
  if (value === null) return "—";
  return Number(value).toLocaleString("pt-BR", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 3,
  });
}

export const KIND_LABEL: Record<Kind, string> = {
  INCOME: "entrada",
  EXPENSE: "saída",
  TRANSFER: "transferência",
};

export function formatDate(value: string | null): string {
  if (!value) return "—";
  // Data pura (`2026-08-01`) não pode passar por `new Date()`: o JS a interpreta
  // como UTC e, no fuso do Brasil, exibe o dia anterior.
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    const [y, m, d] = value.split("-");
    return `${d}/${m}/${y}`;
  }
  return new Date(value).toLocaleString("pt-BR", {
    dateStyle: "short",
    timeStyle: "short",
  });
}
