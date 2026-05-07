import { Brand } from "@/components/Brand";
import { DrillDown } from "@/components/DrillDown";
import { EventStream } from "@/components/EventStream";
import { KPIStrip } from "@/components/KPIStrip";
import { PayerHeader } from "@/components/PayerHeader";
import { PayerRail } from "@/components/PayerRail";
import { TrustChart } from "@/components/TrustChart";
import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

interface PageProps {
  searchParams: { payer?: string };
}

export default async function Page({ searchParams }: PageProps) {
  const [payerList, kpis] = await Promise.all([api.payers(), api.kpis()]);

  const activeId =
    (searchParams.payer && payerList.payers.find((p) => p.payer_id === searchParams.payer)
      ? searchParams.payer
      : payerList.payers[0]?.payer_id) || null;

  if (!activeId) {
    return (
      <main className="px-10 py-12">
        <Brand />
        <p className="mt-12 font-display text-2xl font-light text-parchment-300">
          No payers in the system yet.
        </p>
        <p className="mt-2 font-mono text-[12px] uppercase tracking-widest text-parchment-400">
          Run the seed script: <code>uv run python -m scripts.seed_unified_demo</code>
        </p>
      </main>
    );
  }

  const [detail, timeline, trust] = await Promise.all([
    api.payer(activeId),
    api.timeline(activeId, 60),
    api.trustHistory(activeId, 60),
  ]);

  return (
    <main className="mx-auto max-w-[1480px] px-6 py-8 lg:px-10 lg:py-10">
      <header className="flex items-end justify-between border-b border-ink-500 pb-6">
        <Brand />
        <div className="flex items-baseline gap-4 font-mono text-[10px] uppercase tracking-widest text-parchment-400">
          <span>Cross-vertical signal {kpis.fleet.payers} payers</span>
          <span className="h-1 w-1 rounded-full bg-accent-500" />
          <span>{new Date().toLocaleString()}</span>
        </div>
      </header>

      <section className="mt-6">
        <KPIStrip kpis={kpis} />
      </section>

      <section className="mt-8 grid gap-6 lg:grid-cols-12">
        <aside className="lg:col-span-3">
          <PayerRail payers={payerList.payers} activeId={activeId} />
        </aside>

        <div className="flex flex-col gap-6 lg:col-span-9">
          <PayerHeader detail={detail} />
          <div className="grid gap-6 lg:grid-cols-3">
            <div className="lg:col-span-2">
              <TrustChart
                payerName={detail.name}
                points={trust.points}
                threshold={detail.auto_match_threshold}
              />
            </div>
            <div className="lg:col-span-1">
              <EventStream events={timeline.events} />
            </div>
          </div>
          <DrillDown detail={detail} />
        </div>
      </section>

      <footer className="mt-16 flex items-center justify-between border-t border-ink-500 pt-6 font-mono text-[10px] uppercase tracking-widest text-parchment-400">
        <span>Iridium / invoice-agent-stack</span>
        <span>
          voice <span className="text-accent-500">●</span> &nbsp; browser{" "}
          <span className="text-signal-blue">●</span> &nbsp; recon{" "}
          <span className="text-signal-green">●</span>
        </span>
      </footer>
    </main>
  );
}
