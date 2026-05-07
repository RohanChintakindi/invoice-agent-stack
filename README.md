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
- [x] Cash recon: synth + features + ranker + bundler + ER + service + server
- [x] Cash recon CLI demo
- [x] Ops dashboard: FastAPI aggregator + Next.js / Tailwind frontend
- [x] Unified demo + Vercel snapshot deploy

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

## Running the cash reconciliation demo

```bash
# (Optional) seed acme so the trust score reflects voice agent history.
uv run python -m scripts.seed_demo

# Seed open invoices + payer aliases for cash_recon.
uv run python -m scripts.seed_recon

# Train ranker (first run only, ~1s) + ingest 4 representative wires:
# clean / partial / bundle / decoy. Shows the trust-aware threshold.
uv run python -m scripts.cash_recon_demo

# FastAPI server (POST /wires, GET /reviews, etc.)
uv run uvicorn cash_recon.server:app --reload
```

Pipeline: `wire -> entity resolution -> candidates -> XGBoost+isotonic
ranker -> auto-post or queue review -> trust events`. Auto-match
threshold is **trust-aware**: low-trust payers require ~0.97
calibrated probability, high-trust payers ~0.88.

## Running the unified ops dashboard

The dashboard is a Next.js 14 + Tailwind frontend backed by a FastAPI
JSON aggregator that reads the same SQLite DB as all three verticals.

```bash
# 1. seed activity across all 3 verticals so there's something to show
uv run python -m scripts.seed_unified_demo

# 2. (optional) regenerate the static JSON snapshot used for Vercel
uv run python -m scripts.export_dashboard_snapshot

# 3. local dev (FastAPI on :8765, Next on :3000):
uv run python -m scripts.run_dashboard

# 4. or run the Next app standalone in snapshot mode (no Python needed)
cd ops_dashboard/web && npm install && npm run dev
# open http://localhost:3000
```

The dashboard renders a per-payer trust evolution chart with each
trust event marked by vertical (gold = voice, blue-grey = browser,
green = recon), an activity stream fusing calls + scrape jobs +
wire matches, and drill-down panels for each vertical.

## Deploying the dashboard to Vercel

The Vercel deployment serves the bundled JSON snapshot — no Python
backend is needed at the edge.

```bash
# refresh the snapshot before each deploy
uv run python -m scripts.seed_unified_demo
uv run python -m scripts.export_dashboard_snapshot

# deploy from the web app subdirectory
cd ops_dashboard/web
vercel login    # one-time, opens a browser
vercel --prod   # answers default to most prompts
```

Or connect the GitHub repo via the Vercel web UI with **root
directory** set to `ops_dashboard/web` — Vercel will auto-detect Next.js
and build straight from the snapshot in `public/snapshot/`.

## Tests

```bash
uv run pytest -q
```
