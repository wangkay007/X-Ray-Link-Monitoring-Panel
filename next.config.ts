import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  typescript: { tsconfigPath: "tsconfig.self-hosted.json" },
  experimental: { cpus: 1 },
};

export default nextConfig;
