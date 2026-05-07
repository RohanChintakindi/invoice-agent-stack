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
- [x] Voice agent L3: state machine
- [x] Voice agent L4: memory
- [x] Trust engine
- [x] Voice agent L5: compliance filter
- [x] Voice agent prompts + signal classifier
- [x] Voice agent L2: LangGraph orchestrator + FastAPI/Vapi server
- [x] Voice agent CLI replay driver
- [x] Browser orchestration: queue, vault, validator, worker, server
- [x] Browser orchestration CLI demo
- [ ] Cash recon (vertical 3)
- [ ] Ops dashboard
- [ ] Unified demo

## Setup

```bash
uv sync
cp .env.example .env  # fill in ANTHROPIC_API_KEY, VAPI_API_KEY
```

## Running the voice agent

```bash
# Seed the demo payer (Acme Corp with realistic call history)
uv run python -m scripts.seed_demo

# Text-mode REPL (no Vapi needed, uses Anthropic for the LLM)
uv run python -m scripts.voice_repl --payer acme --invoice INV-1023 --days-overdue 28

# Or with the FakeLLMClient (no API key required, deterministic)
uv run python -m scripts.voice_repl --fake-llm

# FastAPI server (Vapi-compatible /v1/chat/completions)
uv run uvicorn voice_agent.server:app --reload
```

## Running the browser orchestration demo

```bash
# Generate a Fernet key for the credential vault. In prod this comes
# from a real secrets manager.
uv run python -c "from browser_orchestration.vault import generate_key; print(generate_key())"
# Then export it (PowerShell):
$env:VAULT_KEY = "<paste the key>"

# Seed the demo: payer "acme" + two portals (one happy, one silent-fail)
uv run python -m scripts.seed_demo
uv run python -m scripts.seed_portals

# End-to-end demo: enqueues both jobs, runs the worker, prints outcomes
# and shows how SILENT_FAIL_CAUGHT writes back to the trust score.
uv run python -m scripts.browser_demo

# FastAPI server (queue + portal health + scrape-interval endpoints)
uv run uvicorn browser_orchestration.server:app --reload
```

The demo shows the cross-vertical story end-to-end: the voice agent's
`PROMISE_BROKEN` event and the browser layer's `SILENT_FAIL_CAUGHT`
event both write into the same per-payer trust score, which then
drives the next scrape interval.

## Tests

```bash
uv run pytest -q
```
