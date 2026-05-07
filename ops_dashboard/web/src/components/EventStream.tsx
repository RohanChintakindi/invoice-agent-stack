import type { TimelineEvent } from "@/lib/types";

const VERTICAL_COLORS: Record<string, string> = {
  voice: "bg-accent-500",
  browser: "bg-signal-blue",
  recon: "bg-signal-green",
  shared: "bg-parchment-400",
};

function relTime(ts: string | null): string {
  if (!ts) return "—";
  const d = new Date(ts);
  const now = Date.now();
  const dt = (now - d.getTime()) / 1000;
  if (dt < 60) return `${Math.floor(dt)}s ago`;
  if (dt < 3600) return `${Math.floor(dt / 60)}m ago`;
  if (dt < 86400) return `${Math.floor(dt / 3600)}h ago`;
  return `${Math.floor(dt / 86400)}d ago`;
}

export function EventStream({ events }: { events: TimelineEvent[] }) {
  if (events.length === 0) {
    return (
      <div className="bg-ink-800 px-5 py-6 ring-1 ring-inset ring-ink-500">
        <div className="font-mono text-[10px] uppercase tracking-widest text-parchment-400">
          Activity
        </div>
        <p className="mt-3 font-display text-base text-parchment-300">
          No events in this window.
        </p>
      </div>
    );
  }

  return (
    <div className="bg-ink-800 ring-1 ring-inset ring-ink-500">
      <div className="flex items-center justify-between border-b border-ink-500 px-5 py-3">
        <span className="font-mono text-[10px] uppercase tracking-widest text-parchment-400">
          Activity stream
        </span>
        <span className="font-mono text-[10px] tabular text-parchment-400">
          {events.length.toString().padStart(3, "0")}
        </span>
      </div>

      <ol className="max-h-[520px] overflow-y-auto">
        {events.map((e, idx) => {
          const dotColor = VERTICAL_COLORS[e.vertical] || "bg-parchment-400";
          const deltaClass =
            e.delta == null
              ? "text-parchment-300"
              : e.delta > 0
                ? "text-signal-green"
                : "text-signal-red";
          return (
            <li
              key={`${e.ts}-${idx}`}
              className="flex gap-3 border-b border-ink-500/60 px-5 py-3 last:border-b-0 animate-fade-up"
              style={{ animationDelay: `${Math.min(idx, 12) * 30}ms` }}
            >
              <div className="relative mt-1.5">
                <span
                  className={[
                    "block h-1.5 w-1.5 rounded-full ring-4 ring-ink-800",
                    dotColor,
                  ].join(" ")}
                />
              </div>
              <div className="flex min-w-0 flex-1 flex-col">
                <div className="flex items-baseline justify-between gap-3">
                  <span className="font-mono text-[11px] uppercase tracking-wider text-parchment-200">
                    {e.kind}
                  </span>
                  <span className="font-mono text-[10px] tabular text-parchment-400">
                    {relTime(e.ts)}
                  </span>
                </div>
                <p className="mt-1 truncate font-sans text-[13px] text-parchment-100">
                  {e.summary}
                </p>
                {e.delta !== null && e.delta !== undefined && (
                  <div className={`mt-1 font-mono text-[11px] tabular ${deltaClass}`}>
                    delta {e.delta > 0 ? "+" : ""}
                    {e.delta.toFixed(3)}
                  </div>
                )}
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
