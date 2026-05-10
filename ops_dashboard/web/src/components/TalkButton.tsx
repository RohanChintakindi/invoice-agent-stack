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
      // Print the raw object so we can see the real shape in DevTools.
      // The SDK varies what fields it populates — sometimes `error`,
      // `errorMsg`, `message`, sometimes a nested action.error.
      // eslint-disable-next-line no-console
      console.error("[Vapi error]", e);
      const obj = (e ?? {}) as Record<string, unknown>;
      const candidates = [
        obj.errorMsg,
        obj.message,
        (obj.error as Record<string, unknown> | undefined)?.message,
        (obj.error as Record<string, unknown> | undefined)?.errorMsg,
        obj.errorType,
        obj.code,
      ];
      const friendly = candidates.find(
        (v) => typeof v === "string" && v.length > 0,
      );
      const fallback = (() => {
        try {
          return JSON.stringify(e).slice(0, 200);
        } catch {
          return String(e);
        }
      })();
      setError(typeof friendly === "string" ? friendly : fallback);
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
      // Render the system prompt client-side with the real payer baked in.
      // Vapi's `variableValues` substitution into `{{payer_id}}` doesn't
      // fire reliably for Web SDK calls, so we override `model.messages`
      // with literal content the LLM webhook can extract via its
      // [payer_id=...] regex.
      await vapiRef.current.start(VAPI_ASSISTANT_ID, {
        model: {
          provider: "custom-llm",
          model: "claude-haiku-4-5-20251001",
          url: "https://invoice-agent-stack-169815310866.us-central1.run.app/voice/v1",
          messages: [
            {
              role: "system",
              content: `You are an Iridium accounts-receivable agent calling ${payerName} about a past-due invoice. Stay professional and human. The current call is about payer [payer_id=${payerId}].`,
            },
          ],
        },
        firstMessage: `Hi, this is Iridium calling about an outstanding invoice for ${payerName}. Do you have a moment?`,
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
