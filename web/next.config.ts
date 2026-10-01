import type { NextConfig } from "next";

// The browser only talks to this origin; /api/* is proxied to the dot API, so
// the API needs no CORS and the identity proxy (IAP) covers both.
const api = process.env.DOT_API_URL ?? "http://localhost:8000";

const config: NextConfig = {
  output: "standalone",
  // The Playwright suite runs its own dev server beside `pnpm dev`; each needs its own build dir.
  distDir: process.env.DOT_WEB_DIST_DIR ?? ".next",
  reactStrictMode: true,
  // Proxied SSE must not be gzip-buffered.
  compress: false,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${api}/:path*` }];
  },
};

export default config;
