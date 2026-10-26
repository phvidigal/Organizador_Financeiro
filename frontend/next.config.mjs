/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // O código chega por bind mount vindo do Windows, onde o Docker não propaga
  // eventos de filesystem. Sem polling, o hot reload simplesmente não dispara.
  webpack: (config) => {
    config.watchOptions = { poll: 1000, aggregateTimeout: 300 };
    return config;
  },
  // Sem CSP de propósito: o `next dev`, que é o que o compose roda, precisa de
  // `unsafe-eval`, e uma CSP que o permite não protege de nada que importe aqui.
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          // A revisão grava com um clique: dentro de um iframe de outro site, esse
          // clique poderia ser induzido (clickjacking).
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          // As URLs carregam ids de conta e categoria; não há por que mandá-las
          // adiante num link externo.
          { key: "Referrer-Policy", value: "no-referrer" },
        ],
      },
    ];
  },
};

export default nextConfig;
