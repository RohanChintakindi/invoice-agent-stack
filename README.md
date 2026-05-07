# invoice-agent-stack

Three connected agent verticals for invoice-financing operations, sharing a single per-payer trust score that learns from every interaction.

## Verticals

1. **Voice agent** — collections calls with a `(phase, tone)` personality state machine that adapts per payer and intra-call.
2. **Browser orchestration** — production layer on top of [`browser-use/browser-harness`](https://github.com/browser-use/browser-harness): job queue, credential vault, silent-failure validator, observability.
3. **Cash reconciliation** — wire-to-invoice matcher (XGBoost + calibration) with subset-sum bundling, entity resolution, and human-in-loop feedback.

## Connective tissue

A shared `trust_engine` service. Every vertical reads from it (to gate behavior) and writes to it (to update the score):

| Subsystem | Reads to... | Writes when... |
|---|---|---|
| Voice agent | Initialize `(phase, tone)` at call start | Promise kept/broken, hostile call |
| Browser layer | Decide scrape frequency | Silent-fail caught, clean weeks |
| Cash recon | Set auto-match confidence threshold | Confirmed match, human override |

Trust events from any vertical influence behavior in the others.

## Repo layout

```
invoice-agent-stack/
├── shared/                  # trust engine, invoice DB, event bus
├── voice_agent/             # vertical 1
├── browser_orchestration/   # vertical 2 (uses browser-harness)
├── cash_recon/              # vertical 3
├── ops_dashboard/           # unified view
└── docs/
```

## Status

- [x] Repo scaffold
- [ ] Voice agent L3: state machine
- [ ] Voice agent L4: memory
- [ ] Trust engine stub
- [ ] Voice agent L2: orchestrator + Vapi
- [ ] Voice agent L5: compliance
- [ ] Browser orchestration
- [ ] Cash recon
- [ ] Ops dashboard
- [ ] Unified demo

## Setup

```bash
uv sync
cp .env.example .env  # fill in ANTHROPIC_API_KEY, VAPI_API_KEY
```
