import Link from "next/link";
import type { PayerSummary } from "@/lib/types";

function trustClass(score: number) {
  if (score < 0.35) return "text-signal-red";
  if (score < 0.55) return "text-accent-500";
  return "text-signal-green";
}

function trustLabel(score: number) {
  if (score < 0.35) return "AT RISK";
  if (score < 0.55) return "WATCH";
  if (score < 0.75) return "STABLE";
  return "TRUSTED";
}

export function PayerRail({
  payers,
  activeId,
}: {
  payers: PayerSummary[];
  activeId: string;
}) {
  return (
    <nav aria-label="payers" className="flex flex-col">
      <div className="flex items-baseline justify-between border-b border-ink-500 px-1 pb-3">
        <span className="font-mono text-[10px] uppercase tracking-widest text-parchment-400">
          Payers
        </span>
        <span className="font-mono text-[10px] tabular text-parchment-400">
          {payers.length.toString().padStart(2, "0")}
        </span>
      </div>
      <ul className="mt-2 flex flex-col">
        {payers.map((p, idx) => {
          const active = p.payer_id === activeId;
          return (
            <li key={p.payer_id}>
              <Link
                href={`/?payer=${p.payer_id}`}
                className={[
                  "group block px-3 py-3 transition-colors animate-fade-up",
                  active
                    ? "bg-ink-700 ring-inset ring-1 ring-accent-500/40"
                    : "hover:bg-ink-800",
                ].join(" ")}
                style={{ animationDelay: `${idx * 40}ms` }}
              >
                <div className="flex items-center justify-between">
                  <span
                    className={[
                      "font-display text-base font-light",
                      active ? "text-parchment-50" : "text-parchment-100",
                    ].join(" ")}
                  >
                    {p.name}
                  </span>
                  <span
                    className={[
                      "font-mono text-[10px] uppercase tracking-widest",
                      trustClass(p.trust_score),
                    ].join(" ")}
                  >
                    {trustLabel(p.trust_score)}
                  </span>
                </div>
                <div className="mt-2 flex items-center justify-between font-mono text-[11px] tabular text-parchment-400">
                  <span>{p.payer_id}</span>
                  <span className={trustClass(p.trust_score)}>
                    {p.trust_score.toFixed(3)}
                  </span>
                </div>
                <TrustBar score={p.trust_score} />
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

function TrustBar({ score }: { score: number }) {
  const pct = Math.max(0, Math.min(1, score));
  return (
    <div className="mt-2 h-[2px] w-full bg-ink-500">
      <div
        className={[
          "h-full origin-left scale-x-0 animate-ticker",
          trustClass(score).replace("text-", "bg-"),
        ].join(" ")}
        style={{
          transform: `scaleX(${pct})`,
        }}
      />
    </div>
  );
}
