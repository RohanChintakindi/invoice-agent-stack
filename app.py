"""Unified ASGI entrypoint for the Fly.io deployment.

Mounts every vertical's FastAPI app under a path prefix so a single
Fly app exposes all of them on one hostname:

    https://invoice-agent-stack.fly.dev/voice/v1/chat/completions
    https://invoice-agent-stack.fly.dev/browser/jobs
    https://invoice-agent-stack.fly.dev/recon/wires
    https://invoice-agent-stack.fly.dev/ops/payers

Each sub-app has its own lifespan (DB engine, ranker load, harness
wiring). Mounted sub-apps don't have their lifespans triggered by
the parent automatically, so we compose them here through an
AsyncExitStack — each vertical's startup runs in turn, and they all
shut down cleanly in reverse order.

Run locally:
    uv run uvicorn app:main --host 0.0.0.0 --port 8000

In Fly: the Dockerfile invokes the same uvicorn command on $PORT.
"""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager

import os

from dotenv import load_dotenv

load_dotenv()

# Browser orchestration requires a Fernet vault key. In production this
# comes from `fly secrets set VAULT_KEY=...`. For local dev / first-boot
# of an empty Fly app, fall back to an ephemeral key with a warning so
# the service still starts. Anything stored in the vault under an
# ephemeral key is unreadable after restart, which is fine — the demo
# always re-seeds.
if not os.getenv("VAULT_KEY"):
    from browser_orchestration.vault import generate_key

    os.environ["VAULT_KEY"] = generate_key()
    print("[app] WARNING: VAULT_KEY was not set; generated an ephemeral key.")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from browser_orchestration.server import create_app as create_browser_app
from cash_recon.server import create_app as create_recon_app
from demo_portal.server import create_app as create_portal_app
from ops_dashboard.api import create_app as create_ops_app
from voice_agent.server import app as voice_app  # module-level FastAPI instance

# Build sub-apps once at import time.
browser_app = create_browser_app()
recon_app = create_recon_app()
ops_app = create_ops_app()
portal_app = create_portal_app()

_subapps = (voice_app, browser_app, recon_app, ops_app, portal_app)


def _seed_if_empty() -> None:
    """Auto-seed structural data on cold start when the DB is empty.

    Cloud Run scales to zero and uses ephemeral /tmp storage, so each
    cold-started instance gets a fresh DB. Idempotent on a non-empty DB.
    """
    from sqlmodel import select

    from shared.db import init_schema, make_engine, session_scope
    from shared.models import Payer

    engine = make_engine()
    init_schema(engine)
    with session_scope(engine) as s:
        if s.exec(select(Payer)).first() is not None:
            return

    print("[app] empty DB — running seed_unified_demo")
    from scripts.seed_unified_demo import main as seed_main

    seed_main()


@asynccontextmanager
async def _composed_lifespan(_: FastAPI):
    """Run each sub-app's lifespan in sequence, tear down in reverse."""
    async with AsyncExitStack() as stack:
        for sub in _subapps:
            await stack.enter_async_context(sub.router.lifespan_context(sub))
        _seed_if_empty()
        yield


main = FastAPI(
    title="Iridium / invoice-agent-stack",
    description=(
        "Unified deployment of all four verticals. Each is mounted under "
        "its own path prefix and runs its own startup hooks."
    ),
    lifespan=_composed_lifespan,
)

# Wide-open CORS for the demo. Tighten to specific origins in prod.
main.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@main.get("/")
def root() -> dict[str, str]:
    return {
        "service": "invoice-agent-stack",
        "voice_agent": "/voice/health",
        "browser_orchestration": "/browser/health",
        "cash_recon": "/recon/health",
        "ops_api": "/ops/health",
        "demo_portal": "/portal/",
    }


@main.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


main.mount("/voice", voice_app)
main.mount("/browser", browser_app)
main.mount("/recon", recon_app)
main.mount("/ops", ops_app)
main.mount("/portal", portal_app)
