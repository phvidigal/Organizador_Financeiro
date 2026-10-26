// Recusa request cujo `Host` não é a própria máquina.
//
// É a mesma defesa do `TrustedHostMiddleware` do backend, e pelo mesmo motivo: DNS
// rebinding. Uma página maliciosa faz o próprio domínio resolver para 127.0.0.1 e
// lê as telas — que são renderizadas no servidor com o extrato dentro do HTML. Para
// o navegador é same-origin, então nada mais a impediria; o `Host`, porém, continua
// sendo o domínio dela.
//
// Lista fixa e não variável de ambiente, de propósito: não há login, e abrir o app
// para a rede tem de passar por uma mudança de código que traga a autenticação
// junto (ver `Settings._hosts_are_local` no backend).

import { type NextRequest, NextResponse } from "next/server";

const HOSTS_LOCAIS = new Set(["localhost", "127.0.0.1", "[::1]"]);

/** "localhost:3000" → "localhost"; "[::1]:3000" → "[::1]". */
function semPorta(host: string): string {
  if (host.startsWith("[")) return host.slice(0, host.indexOf("]") + 1);
  return host.split(":")[0];
}

export function middleware(request: NextRequest) {
  const host = semPorta(request.headers.get("host") ?? "").toLowerCase();
  if (!HOSTS_LOCAIS.has(host)) {
    return new NextResponse("host não autorizado", { status: 403 });
  }
  return NextResponse.next();
}

export const config = {
  // Os chunks estáticos não carregam dado do titular, e passar cada um pelo
  // middleware só custaria tempo no `next dev`.
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
