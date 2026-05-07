import type { PayerDetail } from "@/lib/types";

function fmtDate(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function Panel({
  title,
  accent,
  children,
  empty,
  count,
}: {
  title: string;
  accent: string;
  children: React.ReactNode;
  empty?: boolean;
  count: number;
}) {
  return (
    <section className="bg-ink-800 ring-1 ring-inset ring-ink-500">
      <div className="flex items-center justify-between border-b border-ink-500 px-5 py-3">
        <div className="flex items-center gap-2">
          <span
            className="inline-block h-1.5 w-1.5 rounded-full"
            style={{ background: accent }}
          />
          <span className="font-mono text-[10px] uppercase tracking-widest text-parchment-400">
            {title}
          </span>
        </div>
        <span className="font-mono text-[10px] tabular text-parchment-400">
          {count.toString().padStart(2, "0")}
        </span>
      </div>
      <div className="px-5 py-4">
        {empty ? (
          <p className="font-display text-sm font-light text-parchment-300">
            No records yet.
          </p>
        ) : (
          children
        )}
      </div>
    </section>
  );
}

function VoicePanel({ detail }: { detail: PayerDetail }) {
  return (
    <Panel
      title="Voice / collections calls"
      accent="#D4A24C"
      count={detail.calls.length}
      empty={detail.calls.length === 0}
    >
      <ul className="flex flex-col divide-y divide-ink-500/60">
        {detail.calls.slice(0, 6).map((c) => (
          <li key={c.id} className="py-3">
            <div className="flex items-baseline justify-between gap-3">
              <span className="font-mono text-[11px] uppercase tracking-wider text-parchment-200">
                {c.outcome}
              </span>
              <span className="font-mono text-[10px] tabular text-parchment-400">
                {fmtDate(c.occurred_at)}
              </span>
            </div>
            <p className="mt-1 font-sans text-[13px] leading-snug text-parchment-100">
              {c.summary}
            </p>
            <div className="mt-1 flex flex-wrap items-center gap-3 font-mono text-[10px] uppercase tracking-widest text-parchment-400">
              {c.contact_name && <span>contact / {c.contact_name}</span>}
              {c.invoice_id && <span>invoice / {c.invoice_id}</span>}
              {c.final_phase && (
                <span>
                  phase / {c.final_phase}
                  {c.final_tone ? `, ${c.final_tone}` : ""}
                </span>
              )}
            </div>
          </li>
        ))}
      </ul>
      {detail.promises.length > 0 && (
        <div className="mt-4 rounded-sm border border-ink-500 px-3 py-3">
          <div className="font-mono text-[10px] uppercase tracking-widest text-parchment-400">
            Promises
          </div>
          <ul className="mt-2 grid grid-cols-1 gap-1 font-mono text-[12px] tabular text-parchment-200">
            {detail.promises.slice(0, 4).map((p) => (
              <li key={p.id} className="flex justify-between">
                <span>
                  {p.invoice_id || "—"} / ${p.promised_amount?.toFixed(0) || "—"}
                </span>
                <span
                  className={
                    p.kept === true
                      ? "text-signal-green"
                      : p.kept === false
                        ? "text-signal-red"
                        : "text-parchment-400"
                  }
                >
                  {p.kept === true
                    ? "kept"
                    : p.kept === false
                      ? "broken"
                      : "open"}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Panel>
  );
}

function BrowserPanel({ detail }: { detail: PayerDetail }) {
  const empty = detail.jobs.length === 0 && detail.extractions.length === 0;
  return (
    <Panel
      title="Browser / portal scrapes"
      accent="#5478A6"
      count={detail.jobs.length}
      empty={empty}
    >
      {detail.jobs.length > 0 && (
        <table className="w-full font-mono text-[11px] tabular">
          <thead>
            <tr className="text-parchment-400">
              <th className="py-1 text-left font-normal uppercase tracking-widest text-[10px]">
                portal
              </th>
              <th className="py-1 text-left font-normal uppercase tracking-widest text-[10px]">
                action
              </th>
              <th className="py-1 text-left font-normal uppercase tracking-widest text-[10px]">
                status
              </th>
              <th className="py-1 text-right font-normal uppercase tracking-widest text-[10px]">
                attempts
              </th>
              <th className="py-1 text-right font-normal uppercase tracking-widest text-[10px]">
                finished
              </th>
            </tr>
          </thead>
          <tbody>
            {detail.jobs.slice(0, 8).map((j) => {
              const statusColor =
                j.status === "succeeded"
                  ? "text-signal-green"
                  : j.status === "silent_fail"
                    ? "text-signal-red"
                    : j.status === "failed"
                      ? "text-signal-red"
                      : "text-parchment-200";
              return (
                <tr key={j.id} className="border-t border-ink-500/60">
                  <td className="py-2 text-parchment-100">{j.portal_id}</td>
                  <td className="py-2 text-parchment-200">{j.action}</td>
                  <td className={`py-2 ${statusColor}`}>{j.status}</td>
                  <td className="py-2 text-right text-parchment-200">
                    {j.attempts}
                  </td>
                  <td className="py-2 text-right text-parchment-400">
                    {fmtDate(j.finished_at)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

function ReconPanel({ detail }: { detail: PayerDetail }) {
  return (
    <Panel
      title="Recon / wire matches"
      accent="#5DBB7A"
      count={detail.wires.length}
      empty={detail.wires.length === 0}
    >
      <ul className="flex flex-col divide-y divide-ink-500/60">
        {detail.wires.slice(0, 6).map((w) => {
          const statusColor =
            w.status === "auto_matched"
              ? "text-signal-green"
              : w.status === "matched"
                ? "text-signal-green"
                : w.status === "under_review"
                  ? "text-accent-500"
                  : w.status === "rejected" || w.status === "unmatched"
                    ? "text-parchment-400"
                    : "text-parchment-200";
          return (
            <li key={w.wire_id} className="py-3">
              <div className="flex items-baseline justify-between">
                <div className="font-mono text-[11px] tabular text-parchment-200">
                  {w.wire_id}
                </div>
                <div className="font-display text-lg font-light tabular text-parchment-50">
                  ${w.amount.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                </div>
              </div>
              <div className="mt-1 flex items-center justify-between font-mono text-[10px] uppercase tracking-widest">
                <span className="text-parchment-400">{w.sender_name}</span>
                <span className={statusColor}>{w.status}</span>
              </div>
              <p className="mt-1 truncate font-sans text-[12px] text-parchment-300">
                {w.memo}
              </p>
              {w.match && (
                <div className="mt-1 font-mono text-[11px] tabular text-parchment-300">
                  → {w.match.invoice_ids}
                  <span className="ml-2 text-parchment-400">
                    cal {w.match.confidence.toFixed(3)} / {w.match.outcome}
                  </span>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}

export function DrillDown({ detail }: { detail: PayerDetail }) {
  return (
    <div className="grid gap-4 md:grid-cols-3">
      <VoicePanel detail={detail} />
      <BrowserPanel detail={detail} />
      <ReconPanel detail={detail} />
    </div>
  );
}
