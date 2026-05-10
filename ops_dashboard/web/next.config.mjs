/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    // Strip BOM + whitespace; Vercel CLI's `env add` via stdin on Windows
    // prepends a UTF-8 BOM to piped values, which breaks Next's rewrite
    // validator (destination must start with /, http://, or https://).
    const apiBase = (process.env.OPS_API_BASE || "http://127.0.0.1:8765")
      .replace(/^﻿/, "")
      .trim();
    return [
      {
        source: "/api/:path*",
        destination: `${apiBase}/:path*`,
      },
    ];
  },
};

export default nextConfig;
