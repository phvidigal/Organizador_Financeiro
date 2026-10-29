"use client";

// O controle que produz `category_source = 'MANUAL'`.
//
// É a peça mais importante da Fase 4, e não por UX: é a única do sistema que gera
// a régua com que o acerto do LLM é medido, a base da pipeline de regras e a
// resposta às perguntas que o modelo faz baixando a confiança. Atrito aqui custa o
// dado de que as Fases 4 e 5 dependem — daí os dois botões.
//
// **"Confirmar" não é enfeite.** Aceitar a sugestão do LLM também grava MANUAL, e
// também vira linha de `categorization_reviews`. Sem isso, só as discordâncias
// seriam registradas, e a taxa de acerto teria numerador sem denominador.
//
// **"Sempre categorizar assim"** fecha o laço: a resposta vira uma regra, aplicada
// na hora ao que o LLM decidiu e ao que está na fila, e o mesmo trecho não é mais
// perguntado. O trecho vem sugerido pelo backend e é editável — quem sabe o que
// se repete no próximo Pix é o titular.

import { useRouter } from "next/navigation";
import { useState } from "react";

import {
  BROWSER_API,
  type CategorizedTransaction,
  type Category,
  type Kind,
  KIND_LABEL,
  RULE_APPLIED_PARAM,
} from "@/lib/api";

const KINDS: Kind[] = ["EXPENSE", "INCOME", "TRANSFER"];

export function CategoryPicker({
  transactionId,
  categories,
  currentCategoryId,
  currentKind,
  suggestedPattern = null,
  // Só a fila de revisão oferece "Confirmar": no extrato a linha já está decidida,
  // e confirmar o que ninguém contestou não é uma resposta.
  offerConfirm = false,
}: {
  transactionId: string;
  categories: Category[];
  currentCategoryId: string | null;
  currentKind: Kind;
  suggestedPattern?: string | null;
  offerConfirm?: boolean;
}) {
  const router = useRouter();
  const [categoryId, setCategoryId] = useState(currentCategoryId ?? "");
  // `kind` é estado próprio, e não derivado da categoria, porque o titular pode
  // sobrepô-lo: um Pix para pagar um serviço pode ser despesa mesmo apontando para
  // outra categoria, e nenhuma origem de dado sabe disso.
  const [kind, setKind] = useState<Kind>(currentKind);
  const [makeRule, setMakeRule] = useState(false);
  const [pattern, setPattern] = useState(suggestedPattern ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Agrupa por raiz para o `<optgroup>`. A ordem vem do backend (rótulo asc), então
  // as filhas já chegam logo depois do pai.
  const groups = new Map<string, Category[]>();
  for (const category of categories) {
    const list = groups.get(category.root_id);
    if (list) list.push(category);
    else groups.set(category.root_id, [category]);
  }
  const rootLabel = (rootId: string) =>
    categories.find((c) => c.id === rootId)?.label ?? "Outras";

  const chosen = categories.find((c) => c.id === categoryId);
  // A regra grava a categoria e herda a natureza dela. Com o `kind` sobreposto, os
  // próximos lançamentos sairiam diferentes deste — o backend recusa, e a tela
  // nem oferece.
  const ruleAllowed = Boolean(chosen) && chosen?.kind === kind;

  function pickCategory(id: string) {
    setCategoryId(id);
    // Trocar a categoria repõe o `kind` dela — o default certo na esmagadora
    // maioria dos casos. Quem quiser divergir sobrepõe depois, e não antes.
    const next = categories.find((c) => c.id === id);
    if (next) setKind(next.kind);
  }

  async function save(id: string, chosenKind: Kind, rulePattern: string | null) {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${BROWSER_API}/transactions/${transactionId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          category_id: id,
          kind: chosenKind,
          ...(rulePattern ? { rule_pattern: rulePattern } : {}),
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`);
        return;
      }
      const body = (await res.json()) as CategorizedTransaction;
      if (body.rule_id) {
        // A navegação já re-renderiza o Server Component, com o aviso.
        const url = new URL(window.location.href);
        url.searchParams.set(RULE_APPLIED_PARAM, String(body.rule_applied ?? 0));
        router.replace(`${url.pathname}?${url.searchParams}`);
      } else {
        // Revalida o Server Component pai: na fila, a linha respondida sai da lista.
        router.refresh();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="picker">
      <select
        value={categoryId}
        onChange={(e) => pickCategory(e.target.value)}
        disabled={busy}
        aria-label="Categoria"
      >
        <option value="">— escolha uma categoria —</option>
        {[...groups.entries()].map(([rootId, items]) => (
          <optgroup key={rootId} label={rootLabel(rootId)}>
            {items.map((category) => (
              <option key={category.id} value={category.id}>
                {category.label}
              </option>
            ))}
          </optgroup>
        ))}
      </select>

      <select
        value={kind}
        onChange={(e) => setKind(e.target.value as Kind)}
        disabled={busy}
        aria-label="Natureza"
      >
        {KINDS.map((k) => (
          <option key={k} value={k}>
            {KIND_LABEL[k]}
          </option>
        ))}
      </select>

      <button
        onClick={() => save(categoryId, kind, makeRule && ruleAllowed ? pattern : null)}
        disabled={busy || !categoryId}
      >
        {busy ? "Salvando…" : "Salvar"}
      </button>

      {offerConfirm && currentCategoryId && (
        <button
          onClick={() => save(currentCategoryId, currentKind, null)}
          disabled={busy}
          title="Aceita a sugestão como está"
        >
          Confirmar
        </button>
      )}

      {suggestedPattern !== null && (
        <label
          className="rule-option"
          title={
            ruleAllowed
              ? "Cria uma regra: os lançamentos com este trecho recebem esta categoria, sem passar pelo LLM"
              : "A regra herda a natureza da categoria — desfaça a troca de natureza para criar uma"
          }
        >
          <input
            type="checkbox"
            checked={makeRule && ruleAllowed}
            onChange={(e) => setMakeRule(e.target.checked)}
            disabled={busy || !ruleAllowed}
          />
          sempre assim com
          <input
            type="text"
            value={pattern}
            onChange={(e) => setPattern(e.target.value)}
            disabled={busy || !makeRule || !ruleAllowed}
            aria-label="Trecho da descrição"
            maxLength={200}
          />
        </label>
      )}

      {error && <span className="status-fail">{error}</span>}
    </div>
  );
}
