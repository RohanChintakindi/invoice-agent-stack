"""End-to-end browser-use demo through the actual worker pipeline.

The earlier scripts/run_browser_use_demo.py invokes BrowserUseAdapter
and the validator directly — proving the harness scrapes correctly but
bypassing the worker. This script closes the loop: jobs go on the
SqliteJobQueue, the worker claims them, runs BrowserUseAdapter, the
validator scores the result, and the worker fires the appropriate
TrustEvent (CLEAN_EXTRACTION_STREAK on pass, SILENT_FAIL_CAUGHT on
silent fail). Final output prints the trust events that fired so you
can verify the cross-vertical loop is wired.

Operates against the live Cloud Run demo portal:
    https://invoice-agent-stack-169815310866.us-central1.run.app/portal/{portal_id}/

Trust events land in a *local* SQLite DB (not Cloud Run's /tmp). To
see them on the production dashboard, the worker process would need
to run on a machine with Chromium that has access to the production
DB — production deployment, not demo.

Run:
    uv sync --extra real-browser
    uv run playwright install chromium
    uv run python -m scripts.run_browser_full_loop
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

# Ensure VAULT_KEY exists before any browser_orchestration import.
if not os.getenv("VAULT_KEY"):
    from browser_orchestration.vault import generate_key

    os.environ["VAULT_KEY"] = generate_key()

from sqlmodel import select

from browser_orchestration.browser_use_adapter import BrowserUseAdapter
from browser_orchestration.models import JobAction, Portal
from browser_orchestration.queue import SqliteJobQueue
from browser_orchestration.vault import CredentialVault
from browser_orchestration.worker import process_one_job
from shared.db import init_schema, make_engine, session_scope
from shared.models import Payer, TrustEventRecord
from voice_agent.llm import FakeLLMClient


SERVICE_BASE = os.getenv(
    "SERVICE_BASE",
    "https://invoice-agent-stack-169815310866.us-central1.run.app",
)
PORTAL_USERNAME = "ap@example"
PORTAL_PASSWORD = "hunter2"

PORTALS_TO_RUN = [
    # (portal_id, payer_id, label)
    ("acme_portal", "acme", "happy path — clean invoice table"),
    ("zenith_portal", "zenith", "silent failure — session expired"),
]


def _seed_minimum(engine, vault: CredentialVault) -> None:
    """Make sure the bare minimum DB rows exist: payers, portals (with
    their base_url pointing at the live Cloud Run demo portal), and vault
    entries with the demo credentials."""
    with session_scope(engine) as s:
        for portal_id, payer_id, _ in PORTALS_TO_RUN:
            if s.get(Payer, payer_id) is None:
                s.add(Payer(payer_id=payer_id, name=payer_id.title()))

            base_url = f"{SERVICE_BASE}/portal/{portal_id}/"
            existing = s.get(Portal, portal_id)
            if existing is None:
                s.add(Portal(
                    portal_id=portal_id, name=portal_id.title(), base_url=base_url
                ))
                s.flush()
            elif existing.base_url != base_url:
                existing.base_url = base_url
                s.add(existing)
                s.flush()

            # Idempotent vault upsert.
            try:
                vault.store(
                    s,
                    portal_id=portal_id,
                    payer_id=payer_id,
                    username=PORTAL_USERNAME,
                    secret=PORTAL_PASSWORD,
                )
            except Exception:
                # Already stored — fine.
                pass


def _enqueue_jobs(engine) -> list[int]:
    job_ids: list[int] = []
    with session_scope(engine) as s:
        q = SqliteJobQueue(s)
        for portal_id, payer_id, _ in PORTALS_TO_RUN:
            # SqliteJobQueue.enqueue returns the new job_id directly.
            job_id = q.enqueue(
                portal_id=portal_id,
                payer_id=payer_id,
                action=JobAction.EXTRACT_INVOICES,
            )
            job_ids.append(job_id)
    return job_ids


def _trust_events_since(engine, since: datetime) -> list[dict]:
    """Return primitive snapshots so the caller can print them after the
    session closes. Reading TrustEventRecord attributes outside the session
    raises sqlalchemy.orm.exc.DetachedInstanceError."""
    with session_scope(engine) as s:
        rows = list(
            s.exec(
                select(TrustEventRecord)
                .where(TrustEventRecord.occurred_at >= since)
                .order_by(TrustEventRecord.occurred_at)
            )
        )
        return [
            {
                "payer_id": r.payer_id,
                "event_type": r.event_type.value,
                "delta": r.delta,
                "source": r.source or "",
            }
            for r in rows
        ]


def main() -> int:
    # Windows default cp1252 stdout chokes on em-dashes etc; force utf-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set; aborting.", file=sys.stderr)
        return 2

    engine = make_engine()
    init_schema(engine)
    vault = CredentialVault.from_env()
    _seed_minimum(engine, vault)

    # Capture cutoff so we can isolate trust events fired during this run.
    started_at = datetime.now(timezone.utc)

    headless = os.getenv("BROWSER_HEADLESS", "false").lower() in ("1", "true", "yes")
    primary_model = os.getenv("BROWSER_USE_MODEL")  # None → adapter default
    print(f"SERVICE_BASE       : {SERVICE_BASE}")
    print(f"headless       : {headless}")
    print(f"primary model  : {primary_model or '(adapter default — Haiku)'}")
    print(f"fallback model : claude-sonnet-4-6")
    print()

    job_ids = _enqueue_jobs(engine)
    print(f"Enqueued {len(job_ids)} jobs: {job_ids}\n")

    # Worker uses a tiny fake LLM for the validator's optional consistency
    # check (we don't need that here — the schema check alone is enough to
    # distinguish acme PASS from zenith FAIL). Keeps the loop fast and
    # deterministic.
    consistency_llm = FakeLLMClient(
        complete_responses=["verdict: pass\nconfidence: 0.9\nwhy: looks fine\n"] * 4
    )

    harness = BrowserUseAdapter(
        model=primary_model,
        headless=headless,
        max_steps=30,
    )

    for portal_id, _payer_id, label in PORTALS_TO_RUN:
        print("=" * 70)
        print(f"  worker.process_one_job  ->  {portal_id}  ({label})")
        print("=" * 70)
        outcome = process_one_job(
            engine=engine, harness=harness, vault=vault, llm=consistency_llm
        )
        if outcome is None:
            print("  (queue was empty — unexpected)\n")
            continue
        print(f"  job_id      : {outcome.job_id}")
        print(f"  status      : {outcome.status.value}")
        print(f"  trust_event : {outcome.trust_event.value if outcome.trust_event else 'none'}")
        if outcome.validation is not None:
            print(f"  verdict     : {outcome.validation.verdict.value}")
            print(f"  rationale   : {outcome.validation.rationale}")
        if outcome.error:
            print(f"  error       : {outcome.error}")
        print()

    print("=" * 70)
    print("  Trust events that fired during this run:")
    print("=" * 70)
    events = _trust_events_since(engine, started_at)
    if not events:
        print("  (none — loop did not fire any trust events)")
    for ev in events:
        print(
            f"  - {ev['payer_id']:8s}  {ev['event_type']:30s}  "
            f"delta={ev['delta']:+.2f}  source={ev['source']}"
        )
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
