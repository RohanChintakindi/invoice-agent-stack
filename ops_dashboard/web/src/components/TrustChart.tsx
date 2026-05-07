"use client";

import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { TrustPoint } from "@/lib/types";

const VERTICAL_COLORS: Record<string, string> = {
  voice: "#D4A24C",
  browser: "#5478A6",
  recon: "#5DBB7A",
  shared: "#787581",
};

function verticalForKind(kind: string): keyof typeof VERTICAL_COLORS {
  if (kind.startsWith("payment.") || kind.startsWith("voice.")) return "voice";
  if (kind.startsWith("browser.")) return "browser";
  if (kind.startsWith("recon.")) return "recon";
  return "shared";
}

interface ChartDatum extends TrustPoint {
  tsMs: number;
  eventScore?: number;
  vertical: string;
}

interface Props {
  payerName: string;
  points: TrustPoint[];
  threshold: number;
}

export function TrustChart({ payerName, points, threshold }: Props) {
  const data: ChartDatum[] = points
    .filter((p) => p.ts)
    .map((p) => {
      const v = verticalForKind(p.kind);
      const isEvent = p.kind !== "baseline" && p.kind !== "now";
      return {
        ...p,
        tsMs: new Date(p.ts).getTime(),
        eventScore: isEvent ? p.score : undefined,
        vertical: v,
      };
    });

  return (
    <div className="relative overflow-hidden bg-ink-800 px-6 py-5 ring-1 ring-inset ring-ink-500 animate-fade-up">
      <div className="flex items-baseline justify-between">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-widest text-parchment-400">
            Trust evolution / cross-vertical
          </div>
          <h2 className="mt-1 font-display text-2xl font-light tracking-tightest text-parchment-50"
              style={{ fontVariationSettings: "'opsz' 144" }}>
            {payerName}
          </h2>
        </div>
        <ChartLegend />
      </div>

      <div className="mt-4 h-[260px] w-full">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart
            data={data}
            margin={{ top: 12, right: 24, bottom: 8, left: -12 }}
          >
            <defs>
              <linearGradient id="trustFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#D4A24C" stopOpacity={0.18} />
                <stop offset="100%" stopColor="#D4A24C" stopOpacity={0} />
              </linearGradient>
            </defs>

            <CartesianGrid
              stroke="#26262E"
              strokeDasharray="2 6"
              vertical={false}
            />
            <XAxis
              dataKey="tsMs"
              type="number"
              domain={["dataMin", "dataMax"]}
              tickFormatter={(t: number) =>
                new Date(t).toLocaleDateString(undefined, {
                  month: "short",
                  day: "numeric",
                })
              }
              stroke="#3A3A45"
              tickLine={false}
              axisLine={false}
            />
            <YAxis
              domain={[0, 1]}
              ticks={[0, 0.25, 0.5, 0.75, 1]}
              tickFormatter={(v) => v.toFixed(2)}
              stroke="#3A3A45"
              tickLine={false}
              axisLine={false}
              width={44}
            />

            <ReferenceLine
              y={threshold}
              stroke="#A07A35"
              strokeDasharray="3 4"
              label={{
                value: `auto-match thr ${threshold.toFixed(3)}`,
                position: "right",
                fill: "#D4A24C",
                fontSize: 10,
                fontFamily: "var(--font-mono)",
              }}
            />

            <Area
              type="monotone"
              dataKey="score"
              stroke="none"
              fill="url(#trustFill)"
              isAnimationActive
            />
            <Line
              type="monotone"
              dataKey="score"
              stroke="#E8E4DA"
              strokeWidth={1.4}
              dot={false}
              isAnimationActive
              animationDuration={900}
            />

            <Scatter
              dataKey="eventScore"
              shape={(props: any) => <EventDot {...props} />}
            />

            <Tooltip
              cursor={{ stroke: "#3A3A45", strokeDasharray: "2 4" }}
              content={<TrustTooltip />}
              wrapperStyle={{ outline: "none" }}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function EventDot(props: any) {
  const { cx, cy, payload } = props;
  if (!cx || !cy || !payload?.eventScore) return null;
  const fill = VERTICAL_COLORS[payload.vertical] || "#D4A24C";
  return (
    <g>
      <circle cx={cx} cy={cy} r={6} fill={fill} fillOpacity={0.18} />
      <circle cx={cx} cy={cy} r={3} fill={fill} />
    </g>
  );
}

function TrustTooltip(props: any) {
  const p = props?.payload?.[0]?.payload;
  if (!p) return null;
  const ts = new Date(p.tsMs).toLocaleString();
  const fill = VERTICAL_COLORS[p.vertical] || "#D4A24C";
  return (
    <div className="bg-ink-700 px-3 py-2 ring-1 ring-inset ring-ink-500 shadow-2xl">
      <div className="font-mono text-[10px] uppercase tracking-widest text-parchment-400">
        {ts}
      </div>
      <div className="mt-1 flex items-center gap-2">
        <span
          className="inline-block h-2 w-2 rounded-full"
          style={{ background: fill }}
        />
        <span className="font-mono text-[11px] text-parchment-100">
          {p.kind}
        </span>
      </div>
      <div className="mt-1 flex items-baseline gap-3 font-mono text-[11px] tabular text-parchment-200">
        <span>score {p.score.toFixed(3)}</span>
        {typeof p.delta === "number" && p.delta !== 0 && (
          <span
            className={
              p.delta > 0 ? "text-signal-green" : "text-signal-red"
            }
          >
            {p.delta > 0 ? "+" : ""}
            {p.delta.toFixed(3)}
          </span>
        )}
      </div>
    </div>
  );
}

function ChartLegend() {
  const items = [
    { label: "voice", color: VERTICAL_COLORS.voice },
    { label: "browser", color: VERTICAL_COLORS.browser },
    { label: "recon", color: VERTICAL_COLORS.recon },
  ];
  return (
    <div className="flex items-center gap-4 font-mono text-[10px] uppercase tracking-widest text-parchment-400">
      {items.map((it) => (
        <span key={it.label} className="flex items-center gap-2">
          <span
            className="inline-block h-1.5 w-1.5 rounded-full"
            style={{ background: it.color }}
          />
          {it.label}
        </span>
      ))}
    </div>
  );
}
