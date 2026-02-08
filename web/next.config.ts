import type { NextConfig } from "next";

const apiInternalBase = (process.env.API_INTERNAL_URL || "http://localhost:8000").replace(/\/$/, "");

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${apiInternalBase}/:path*`,
      },
    ];
  },
};

export default nextConfig;
