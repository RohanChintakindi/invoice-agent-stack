import type { KPIResponse } from "@/lib/types";

function pct(n: number) {
  return `${(n * 100).toFixed(1)}%`;
}

export function KPIStrip({ kpis }: { kpis: KPIResponse }) {
  const items = [
    {
      label: "Fleet trust",
      value: kpis.fleet.avg_trust_score.toFixed(3),
      sub: `${kpis.fleet.payers} payers`,
    },
    {
      label: "Calls",
      value: kpis.voice.calls.toString(),
      sub: "voice agent",
    },
    {
      label: "Browser jobs",
      value: kpis.browser.jobs_total.toString(),
      sub: `${pct(kpis.browser.silent_fail_rate)} silent fail`,
    },
    {
      label: "Wires",
      value: kpis.recon.wires_total.toString(),
      sub: `${pct(kpis.recon.auto_match_rate)} auto-matched`,
    },
    {
      label: "Review queue",
      value: kpis.recon.under_review.toString(),
      sub: `${kpis.recon.human_overrides} overrides`,
    },
  ];

  return (
    <div className="grid grid-cols-2 gap-px overflow-hidden rounded-sm border border-ink-500 bg-ink-500 md:grid-cols-5">
      {items.map((it, i) => (
        <div
          key={it.label}
          className="bg-ink-800 px-5 py-4 animate-fade-up"
          style={{ animationDelay: `${i * 60}ms` }}
        >
          <div className="font-mono text-[10px] uppercase tracking-widest text-parchment-400">
            {it.label}
          </div>
          <div className="mt-1 font-display text-2xl font-light text-parchment-50 tabular tracking-tighter">
            {it.value}
          </div>
          <div className="mt-1 font-mono text-[11px] text-parchment-300 tabular">
            {it.sub}
          </div>
        </div>
      ))}
    </div>
  );
}
