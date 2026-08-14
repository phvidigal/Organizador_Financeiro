// Navegação, num lugar só.
//
// Era uma cópia idêntica em cada página. Fica como componente e não no
// `layout.tsx` porque o `<nav>` vive dentro do `<main>`, e o `<main>` muda de
// largura conforme a tela (`main` vs `main.wide`) — no layout ele precisaria de um
// container próprio e deixaria de acompanhar a coluna da página.

import Link from "next/link";

export function Nav() {
  return (
    <nav className="nav">
      <Link href="/">Diagnóstico</Link>
      <Link href="/conexoes">Conexões</Link>
      <Link href="/transacoes">Transações</Link>
      <Link href="/revisao">Revisão</Link>
    </nav>
  );
}
