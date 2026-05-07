"use client";

import Vapi from "@vapi-ai/web";
import { useEffect, useRef, useState } from "react";

const VAPI_PUBLIC_KEY = process.env.NEXT_PUBLIC_VAPI_PUBLIC_KEY ?? "";
const VAPI_ASSISTANT_ID = process.env.NEXT_PUBLIC_VAPI_ASSISTANT_ID ?? "";

type Phase = "idle" | "connecting" | "live" | "error";

export function TalkButton({ payerId, payerName }: { payerId: string; payerName: string }) {
  const vapiRef = useRef<Vapi | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!VAPI_PUBLIC_KEY) return;
    const vapi = new Vapi(VAPI_PUBLIC_KEY);
    vapi.on("call-start", () => setPhase("live"));
    vapi.on("call-end", () => setPhase("idle"));
    vapi.on("error", (e) => {
      // Surface a short, human-readable reason in the UI.
      const msg =
        typeof e === "string"
          ? e
          : (e as { errorMsg?: string; message?: string })?.errorMsg ??
            (e as { message?: string })?.message ??
            "vapi error";
      setError(msg);
      setPhase("error");
    });
    vapiRef.current = vapi;
    return () => {
      try {
        vapi.stop();
      } catch {
        /* nothing to stop */
      }
    };
  }, []);

  if (!VAPI_PUBLIC_KEY || !VAPI_ASSISTANT_ID) {
    return (
      <div className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-widest text-parchment-400">
        <span className="h-1.5 w-1.5 rounded-full bg-parchment-500" />
        Vapi keys not configured
      </div>
    );
  }

  const start = async () => {
    if (!vapiRef.current) return;
    setError(null);
    setPhase("connecting");
    try {
      // Web SDK takes AssistantOverrides directly as the second arg.
      await vapiRef.current.start(VAPI_ASSISTANT_ID, {
        variableValues: {
          payer_id: payerId,
          payer_name: payerName,
        },
        metadata: {
          payer_id: payerId,
          source: "ops_dashboard.talk_button",
        },
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setPhase("error");
    }
  };

  const stop = () => {
    vapiRef.current?.stop();
    setPhase("idle");
  };

  const label =
    phase === "idle"
      ? `Talk to AR agent (as ${payerName})`
      : phase === "connecting"
        ? "Connecting…"
        : phase === "live"
          ? "End call"
          : "Retry";

  const statusDot =
    phase === "idle"
      ? "bg-accent-500"
      : phase === "connecting"
        ? "bg-signal-blue animate-pulse"
        : phase === "live"
          ? "bg-signal-green animate-pulse"
          : "bg-signal-red";

  return (
    <div className="flex flex-col items-end gap-1">
      <button
        onClick={phase === "live" || phase === "connecting" ? stop : start}
        disabled={phase === "connecting"}
        className="group inline-flex items-center gap-2 rounded-full border border-accent-500/40 bg-ink-700/60 px-4 py-1.5 font-mono text-[10px] uppercase tracking-widest text-accent-500 transition hover:border-accent-500 hover:bg-ink-600 disabled:cursor-wait disabled:opacity-50"
      >
        <span className={`h-1.5 w-1.5 rounded-full ${statusDot}`} />
        {label}
      </button>
      {error ? (
        <span className="font-mono text-[9px] uppercase tracking-wide text-signal-red">
          {error.slice(0, 80)}
        </span>
      ) : null}
    </div>
  );
}
