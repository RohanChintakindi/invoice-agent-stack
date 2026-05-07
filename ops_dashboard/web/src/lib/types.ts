export type Vertical = "voice" | "browser" | "recon" | "shared";

export interface PayerSummary {
  payer_id: string;
  name: string;
  trust_score: number;
  auto_match_threshold: number;
  last_event_at: string | null;
  last_event_type: string | null;
}

export interface PayerListResponse {
  count: number;
  payers: PayerSummary[];
}

export interface KPIResponse {
  fleet: { payers: number; avg_trust_score: number };
  voice: { calls: number };
  browser: {
    jobs_total: number;
    succeeded: number;
    silent_fails: number;
    silent_fail_rate: number;
  };
  recon: {
    wires_total: number;
    auto_matched: number;
    under_review: number;
    unmatched: number;
    auto_match_rate: number;
    human_overrides: number;
  };
}

export interface TimelineEvent {
  ts: string | null;
  vertical: Vertical;
  kind: string;
  summary: string;
  delta: number | null;
  source: string | null;
}

export interface TimelineResponse {
  payer_id: string;
  since_days: number;
  events: TimelineEvent[];
}

export interface TrustPoint {
  ts: string;
  score: number;
  kind: string;
  delta?: number;
  source?: string | null;
}

export interface TrustHistoryResponse {
  payer_id: string;
  since_days: number;
  points: TrustPoint[];
}

export interface PayerDetail {
  payer_id: string;
  name: string;
  trust_score: number;
  auto_match_threshold: number;
  aliases: string[];
  kpis: {
    calls: number;
    promises_kept: number;
    promises_broken: number;
    browser_jobs: number;
    silent_fails: number;
    wires_auto_matched: number;
    wires_under_review: number;
  };
  calls: Array<{
    id: number;
    occurred_at: string | null;
    summary: string;
    outcome: string;
    duration_sec: number | null;
    contact_name: string | null;
    invoice_id: string | null;
    final_phase: string | null;
    final_tone: string | null;
  }>;
  promises: Array<{
    id: number;
    promised_date: string | null;
    promised_amount: number | null;
    invoice_id: string | null;
    kept: boolean | null;
  }>;
  objections: Array<{ kind: string; text: string; occurred_at: string | null }>;
  jobs: Array<{
    id: number;
    portal_id: string;
    action: string;
    status: string;
    attempts: number;
    enqueued_at: string | null;
    finished_at: string | null;
    last_error: string | null;
  }>;
  extractions: Array<{
    id: number;
    portal_id: string;
    invoice_count: number;
    extracted_at: string | null;
  }>;
  wires: Array<{
    wire_id: string;
    amount: number;
    received_on: string;
    memo: string;
    sender_name: string;
    status: string;
    match: { invoice_ids: string; outcome: string; confidence: number } | null;
  }>;
}
