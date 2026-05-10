import type {
  KPIResponse,
  PayerDetail,
  PayerListResponse,
  TimelineResponse,
  TrustHistoryResponse,
} from "./types";
import { readSnapshot, snapshotExists } from "./snapshot";

// Server-side fetch base. Local dev: the FastAPI on 127.0.0.1:8765.
// Vercel: no live API, so the snapshot path takes over.
// BOM strip: Vercel CLI's stdin-piped env values arrive with a UTF-8 BOM.
const SERVER_BASE = (process.env.OPS_API_BASE || "")
  .replace(/^﻿/, "")
  .trim();
// Browser-side: the Next rewrite at /api/* proxies to the FastAPI.
const BROWSER_BASE = "/api";

const FORCE_SNAPSHOT =
  (process.env.NEXT_PUBLIC_USE_SNAPSHOT ?? "")
    .replace(/^﻿/, "")
    .trim() === "1";

async function getJsonLive<T>(path: string): Promise<T> {
  const base = typeof window === "undefined" ? SERVER_BASE : BROWSER_BASE;
  const res = await fetch(`${base}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`api ${path} failed: ${res.status}`);
  return res.json() as Promise<T>;
}

async function get<T>(livePath: string, snapshotFile: string): Promise<T> {
  if (typeof window === "undefined") {
    // Server side. Prefer snapshot if forced or no live base configured.
    if (FORCE_SNAPSHOT || !SERVER_BASE) {
      if (await snapshotExists()) {
        return readSnapshot<T>(snapshotFile);
      }
    }
  }
  return getJsonLive<T>(livePath);
}

export const api = {
  payers: () => get<PayerListResponse>("/payers", "payers.json"),
  payer: (id: string) =>
    get<PayerDetail>(`/payers/${id}`, `payer-${id}.json`),
  timeline: (id: string, sinceDays = 60) =>
    get<TimelineResponse>(
      `/payers/${id}/timeline?since_days=${sinceDays}`,
      `payer-${id}-timeline.json`,
    ),
  trustHistory: (id: string, sinceDays = 60) =>
    get<TrustHistoryResponse>(
      `/payers/${id}/trust-history?since_days=${sinceDays}`,
      `payer-${id}-trust.json`,
    ),
  kpis: () => get<KPIResponse>("/kpis", "kpis.json"),
};
