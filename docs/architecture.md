# Architecture

## The merged design

Three agent verticals, one shared trust engine.

```
                    ┌─────────────────────────┐
                    │   Payer Trust Engine    │
                    │   (shared service)      │
                    └────────┬────────────────┘
                             │ reads/writes
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
 ┌────────────┐      ┌──────────────┐      ┌────────────┐
 │  Browser   │      │  Voice       │      │ Cash Recon │
 │  Orches-   │      │  Agent       │      │  Matcher   │
 │  tration   │      │              │      │            │
 │ (on top of │      │   (Vapi)     │      │ (XGBoost)  │
 │ browser-   │      │              │      │            │
 │ harness)   │      │              │      │            │
 └─────┬──────┘      └──────┬───────┘      └─────┬──────┘
       │                    │                    │
       └────────────────────┼────────────────────┘
                            ▼
                   ┌─────────────────┐
                   │  Invoice DB +   │
                   │   Event Bus     │
                   └────────┬────────┘
                            ▼
                   ┌─────────────────┐
                   │   Unified Ops   │
                   │   Dashboard     │
                   └─────────────────┘
```

## Voice agent (vertical 1) — 5 layers

| Layer | Role | Tech |
|---|---|---|
| L1 | Voice infra | Vapi (webhook in, audio out) |
| L2 | Orchestrator | FastAPI; per-turn LLM call assembling the system prompt from L3+L4 |
| L3 | Personality state machine | `(phase, tone)` finite state, transitions from inter-call + intra-call signals |
| L4 | Memory | Per-payer record (contacts, prior calls, promises, objections) |
| L5 | Compliance | Regex + LLM filter on outgoing utterances |

### Phase × tone

Phases: `friendly_reminder` → `firm_followup` → `escalation` → `pre_legal` → `paused`
Tones: `warm` / `professional` / `firm` / `cold`

State at call start initialized from trust score:

| Trust | Initial phase | Initial tone |
|---|---|---|
| ≥ 0.85 | `friendly_reminder` | `warm` |
| 0.65–0.85 | `friendly_reminder` | `professional` |
| 0.45–0.65 | `firm_followup` | `professional` |
| 0.25–0.45 | `firm_followup` | `firm` |
| < 0.25 | `escalation` | `firm` |

(then bumped further by days-overdue and broken-promise count)

### Intra-call transitions

- Sentiment turns hostile → soften tone one step
- "Manager" / "dispute" / "bankruptcy" → branch to handlers
- Long hesitation + "let me check" → callback-scheduling branch
- Payment commitment given → transition to `paused`

## Trust engine (shared)

API:
- `get_trust(payer_id) -> float` — returns score in [0, 1]
- `update_trust(payer_id, event) -> float` — applies event delta, returns new score

Events (with deltas):

| Event | Δ |
|---|---|
| `payment.promise_kept` | +0.05 |
| `payment.promise_broken` | −0.10 |
| `payment.partial_received` | +0.02 |
| `recon.auto_matched` | +0.01 |
| `recon.human_override` | −0.02 |
| `browser.silent_fail_caught` | −0.03 |
| `browser.clean_extraction_streak` | +0.01 |
| `voice.call_hostile` | −0.02 |

Score clamped to [0, 1]. Decay over time (no signal for 30 days → drift toward 0.5).
