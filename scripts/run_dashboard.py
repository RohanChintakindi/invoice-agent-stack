"""Launch the unified ops dashboard locally.

Spawns the FastAPI on port 8765 and Next.js dev server on port 3000.
On Ctrl-C, both children are terminated.

Usage:
    uv run python -m scripts.seed_unified_demo   # populate the DB
    uv run python -m scripts.run_dashboard

First run: install Node deps with `npm install` inside ops_dashboard/web.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

API_PORT = int(os.getenv("OPS_API_PORT", "8765"))
WEB_DIR = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"


def ensure_node_modules() -> None:
    if not (WEB_DIR / "node_modules").exists():
        print(f"[dashboard] installing node deps into {WEB_DIR}/node_modules...")
        subprocess.run(
            ["npm", "install", "--no-fund", "--no-audit"], cwd=WEB_DIR, check=True,
            shell=sys.platform == "win32",
        )


def main() -> int:
    ensure_node_modules()

    api_cmd = [
        sys.executable, "-m", "uvicorn", "ops_dashboard.api:app",
        "--host", "127.0.0.1", "--port", str(API_PORT),
    ]
    print(f"[dashboard] starting api: {' '.join(api_cmd)}")
    api_proc = subprocess.Popen(api_cmd)

    # Give the API a moment to bind before the web client tries to call it.
    time.sleep(1.5)

    web_env = os.environ.copy()
    web_env["OPS_API_BASE"] = f"http://127.0.0.1:{API_PORT}"
    print(f"[dashboard] starting next dev (proxy -> :{API_PORT})")
    web_proc = subprocess.Popen(
        ["npm", "run", "dev"], cwd=WEB_DIR, env=web_env,
        shell=sys.platform == "win32",
    )

    print()
    print("  ops dashboard:  http://127.0.0.1:3000")
    print(f"  api docs:       http://127.0.0.1:{API_PORT}/docs")
    print()
    print("  press Ctrl-C to stop both processes")

    try:
        web_proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for p in (web_proc, api_proc):
            try:
                p.send_signal(signal.SIGTERM)
            except Exception:
                pass
        for p in (web_proc, api_proc):
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
