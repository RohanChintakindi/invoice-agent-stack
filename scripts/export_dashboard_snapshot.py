"""Export the ops dashboard data as static JSON snapshots.

Used to deploy the dashboard to Vercel (or any host that won't run the
Python backend). Calls each API endpoint via fastapi.testclient and
writes files into ops_dashboard/web/public/snapshot/, mirroring the
API path layout that ops_dashboard/web/src/lib/api.ts expects.

Run after seeding:
    uv run python -m scripts.seed_unified_demo
    uv run python -m scripts.export_dashboard_snapshot
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from ops_dashboard.api import create_app

SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web" / "public" / "snapshot"


def main() -> int:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    app = create_app()
    written: list[str] = []
    with TestClient(app) as c:
        kpis = c.get("/kpis").json()
        _write("kpis.json", kpis, written)

        payers = c.get("/payers").json()
        _write("payers.json", payers, written)

        for p in payers["payers"]:
            pid = p["payer_id"]
            _write(f"payer-{pid}.json", c.get(f"/payers/{pid}").json(), written)
            _write(
                f"payer-{pid}-timeline.json",
                c.get(f"/payers/{pid}/timeline").json(),
                written,
            )
            _write(
                f"payer-{pid}-trust.json",
                c.get(f"/payers/{pid}/trust-history").json(),
                written,
            )

    print(f"[snapshot] wrote {len(written)} files to {SNAPSHOT_DIR}")
    for f in written:
        print("  ", f)
    return 0


def _write(name: str, data: dict, written: list[str]) -> None:
    out = SNAPSHOT_DIR / name
    out.write_text(json.dumps(data, indent=2))
    written.append(name)


if __name__ == "__main__":
    sys.exit(main())
