"use client";

import { useState } from "react";

const BROWSER_API = process.env.NEXT_PUBLIC_BROWSER_API_BASE ?? "";

type Phase = "idle" | "running" | "done" | "error";

interface ScrapeResult {
  job_id?: number;
  status?: string;
  verdict?: string;
  trust_event?: string;
  rationale?: string;
}

const SCENARIO_BY_PAYER: Record<string, { portal: string; label: string }> = {
  acme:   { portal: "acme_portal",   label: "Acme AP portal" },
  zenith: { portal: "zenith_portal", label: "Zenith vendor portal" },
  globex: { portal: "globex_portal", label: "Globex billing portal" },
};

export function BrowserScrapeButton({ payerId }: { payerId: string }) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [result, setResult] = useState<ScrapeResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const scenario = SCENARIO_BY_PAYER[payerId];

  if (!BROWSER_API) {
    return (
      <div className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-widest text-parchment-400">
        <span className="h-1.5 w-1.5 rounded-full bg-parchment-500" />
        Browser API base not configured
      </div>
    );
  }
  if (!scenario) return null;

  const run = async () => {
    setPhase("running");
    setError(null);
    setResult(null);
    try {
      // 1. Enqueue the job (action=extract_invoices).
      const enqueueResp = await fetch(`${BROWSER_API}/jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          portal_id: scenario.portal,
          payer_id: payerId,
          action: "extract_invoices",
        }),
      });
      if (!enqueueResp.ok) throw new Error(`enqueue ${enqueueResp.status}`);
      const { job_id } = (await enqueueResp.json()) as { job_id: number };

      // 2. Run any-ready-job. The endpoint deliberately picks the next ready
      // job rather than enforcing the id, so this works in a single request.
      const runResp = await fetch(`${BROWSER_API}/jobs/${job_id}/run`, {
        method: "POST",
      });
      if (!runResp.ok) throw new Error(`run ${runResp.status}`);
      const out = (await runResp.json()) as ScrapeResult & { ran: boolean };

      setResult(out);
      setPhase(out.verdict === "fail" ? "done" : "done");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setPhase("error");
    }
  };

  const dot =
    phase === "running"
      ? "bg-signal-blue animate-pulse"
      : phase === "done" && result?.verdict === "fail"
        ? "bg-signal-red"
        : phase === "done"
          ? "bg-signal-green"
          : phase === "error"
            ? "bg-signal-red"
            : "bg-accent-500";

  return (
    <div className="flex flex-col items-end gap-1">
      <button
        onClick={run}
        disabled={phase === "running"}
        className="group inline-flex items-center gap-2 rounded-full border border-accent-500/40 bg-ink-700/60 px-4 py-1.5 font-mono text-[10px] uppercase tracking-widest text-accent-500 transition hover:border-accent-500 hover:bg-ink-600 disabled:cursor-wait disabled:opacity-50"
      >
        <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
        {phase === "running"
          ? `Scraping ${scenario.label}…`
          : `Run scrape · ${scenario.label}`}
      </button>
      {result ? (
        <span className="font-mono text-[9px] uppercase tracking-wide text-parchment-400">
          job#{result.job_id} · status={result.status} · verdict={result.verdict}
          {result.trust_event ? ` · trust=${result.trust_event}` : ""}
        </span>
      ) : null}
      {error ? (
        <span className="font-mono text-[9px] uppercase tracking-wide text-signal-red">
          {error.slice(0, 100)}
        </span>
      ) : null}
    </div>
  );
}
