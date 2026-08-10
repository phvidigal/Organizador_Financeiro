/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // O código chega por bind mount vindo do Windows, onde o Docker não propaga
  // eventos de filesystem. Sem polling, o hot reload simplesmente não dispara.
  webpack: (config) => {
    config.watchOptions = { poll: 1000, aggregateTimeout: 300 };
    return config;
  },
};

export default nextConfig;
