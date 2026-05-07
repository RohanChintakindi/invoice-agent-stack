import type { PayerDetail } from "@/lib/types";

function trustClass(score: number) {
  if (score < 0.35) return "text-signal-red";
  if (score < 0.55) return "text-accent-500";
  return "text-signal-green";
}

export function PayerHeader({ detail }: { detail: PayerDetail }) {
  const { kpis } = detail;

  const stats = [
    { label: "calls", value: kpis.calls },
    { label: "promises kept", value: kpis.promises_kept, tone: "good" },
    { label: "promises broken", value: kpis.promises_broken, tone: "bad" },
    { label: "browser jobs", value: kpis.browser_jobs },
    { label: "silent fails", value: kpis.silent_fails, tone: "bad" },
    { label: "wires auto-matched", value: kpis.wires_auto_matched, tone: "good" },
    { label: "wires under review", value: kpis.wires_under_review, tone: "warn" },
  ];

  return (
    <div className="flex flex-wrap items-end justify-between gap-6 border-b border-ink-500 pb-5">
      <div>
        <div className="font-mono text-[10px] uppercase tracking-widest text-parchment-400">
          Selected payer / {detail.payer_id}
        </div>
        <div className="mt-2 flex items-baseline gap-4">
          <h1
            className="font-display text-4xl font-light tracking-tightest text-parchment-50"
            style={{ fontVariationSettings: "'opsz' 144, 'SOFT' 50" }}
          >
            {detail.name}
          </h1>
          <span
            className={[
              "font-mono text-sm tabular tracking-tighter",
              trustClass(detail.trust_score),
            ].join(" ")}
          >
            trust {detail.trust_score.toFixed(3)}
          </span>
        </div>
        {detail.aliases.length > 0 && (
          <div className="mt-2 flex flex-wrap items-center gap-2 font-mono text-[10px] uppercase tracking-widest text-parchment-400">
            <span>aliases</span>
            {detail.aliases.slice(0, 6).map((a) => (
              <span
                key={a}
                className="border border-ink-500 px-2 py-0.5 text-[10px] text-parchment-200"
              >
                {a}
              </span>
            ))}
          </div>
        )}
      </div>

      <div className="grid grid-cols-3 gap-x-8 gap-y-3 sm:grid-cols-4 md:grid-cols-7">
        {stats.map((s) => {
          const colour =
            s.tone === "good"
              ? "text-signal-green"
              : s.tone === "bad"
                ? "text-signal-red"
                : s.tone === "warn"
                  ? "text-accent-500"
                  : "text-parchment-100";
          return (
            <div key={s.label}>
              <div className="font-mono text-[10px] uppercase tracking-widest text-parchment-400">
                {s.label}
              </div>
              <div
                className={`mt-0.5 font-display text-2xl font-light tabular tracking-tighter ${colour}`}
              >
                {s.value}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
