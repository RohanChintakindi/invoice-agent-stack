"""Fake AR portals for end-to-end testing of the browser harness.

Each portal mounts at ``/{portal_id}/`` and renders just enough HTML for
the browser-use agent to log in, navigate, and (sometimes) extract an
invoice table. Three behaviours, deliberately chosen to exercise the
validator that scores harness output:

  - ``acme_portal``    : happy path. Login succeeds, dashboard shows a
                         clean invoice table the agent can scrape.
  - ``zenith_portal``  : silent failure. Login looks like it works (302
                         to dashboard, cookie set), but the dashboard
                         renders a "session expired" banner instead of
                         the table. This is the canonical edge case
                         validator.py catches — harness reports ok=true
                         but no usable data was extracted.
  - ``globex_portal``  : happy path with a single invoice.

Credentials are deliberately the same as the seeded vault entries so
``CredentialVault.get(portal_id, payer_id)`` produces working logins:
``ap@example`` / ``hunter2``.

The server is intentionally template-free — inline HTML strings keep the
DOM small and predictable so the LLM driving browser-use sees clear
landmarks (form names, table classes, status banners).
"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager

from fastapi import Cookie, FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse


VALID_USERNAME = "ap@example"
VALID_PASSWORD = "hunter2"

# Invoices each portal returns when scraped. Mirrors the data
# scripts/seed_unified_demo.py plants in the recon DB so the
# browser-extracted view lines up with what cash recon expects.
PORTAL_INVOICES: dict[str, list[dict]] = {
    "acme_portal": [
        {"invoice_id": "INV-2001", "amount": 12000.00, "due_date": "2026-05-01", "status": "overdue"},
        {"invoice_id": "INV-2002", "amount": 4500.00, "due_date": "2026-05-06", "status": "open"},
        {"invoice_id": "INV-2003", "amount": 7500.00, "due_date": "2026-05-09", "status": "open"},
    ],
    "globex_portal": [
        {"invoice_id": "INV-2006", "amount": 1800.00, "due_date": "2026-05-04", "status": "overdue"},
    ],
    # zenith_portal intentionally absent — it always renders "session expired"
}


# ---- Tiny session cookie ----------------------------------------------------
# Not real auth — just enough to demonstrate the login flow. base64-encoded
# "{portal_id}:{username}" so we can verify the cookie matches the path.

_COOKIE_NAME = "iridium_portal_session"


def _make_session(portal_id: str, username: str) -> str:
    return base64.b64encode(f"{portal_id}:{username}".encode()).decode()


def _check_session(portal_id: str, raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        decoded = base64.b64decode(raw).decode()
        ptl, user = decoded.split(":", 1)
        if ptl == portal_id:
            return user
    except (ValueError, UnicodeDecodeError):
        return None
    return None


# ---- HTML fragments ---------------------------------------------------------


def _login_page(portal_id: str, error: str | None = None) -> str:
    title = portal_id.replace("_", " ").title()
    error_block = (
        f'<div class="error" style="color:#c00;margin:10px 0">{error}</div>'
        if error
        else ""
    )
    # `action="login"` is relative to the current page (e.g. /portal/acme_portal/)
    # so the form submits to /portal/acme_portal/login regardless of mount prefix.
    # Absolute paths like /{portal_id}/login break when this sub-app is mounted
    # under /portal/ on the unified Cloud Run app.
    return f"""<!doctype html>
<html>
<head><title>{title} — Sign in</title></head>
<body style="font-family:system-ui;max-width:480px;margin:60px auto">
  <h1>{title}</h1>
  <p>Sign in to view your accounts-receivable statements.</p>
  {error_block}
  <form method="post" action="login">
    <p>
      <label>Email<br>
        <input type="email" name="username" required style="width:100%;padding:8px">
      </label>
    </p>
    <p>
      <label>Password<br>
        <input type="password" name="password" required style="width:100%;padding:8px">
      </label>
    </p>
    <p><button type="submit" style="padding:8px 24px">Sign in</button></p>
  </form>
</body>
</html>"""


def _invoice_table_page(portal_id: str, invoices: list[dict]) -> str:
    title = portal_id.replace("_", " ").title()
    rows = "".join(
        f'<tr>'
        f'<td>{inv["invoice_id"]}</td>'
        f'<td style="text-align:right">${inv["amount"]:,.2f}</td>'
        f'<td>{inv["due_date"]}</td>'
        f'<td>{inv["status"]}</td>'
        f'</tr>'
        for inv in invoices
    )
    # Relative href so logout works under any mount prefix.
    return f"""<!doctype html>
<html>
<head><title>{title} — Invoices</title></head>
<body style="font-family:system-ui;max-width:800px;margin:40px auto">
  <h1>{title}</h1>
  <p><a href="logout">Sign out</a></p>
  <h2>Open invoices</h2>
  <table id="invoices" border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;width:100%">
    <thead>
      <tr style="background:#eee">
        <th>Invoice</th><th>Amount</th><th>Due date</th><th>Status</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
</body>
</html>"""


def _session_expired_page(portal_id: str) -> str:
    """Silent-failure page for zenith_portal. Page loads (200), no error
    is surfaced visually beyond the banner — exactly the kind of state the
    harness can mistake for a successful scrape if the validator isn't
    paying attention."""
    title = portal_id.replace("_", " ").title()
    return f"""<!doctype html>
<html>
<head><title>{title} — Invoices</title></head>
<body style="font-family:system-ui;max-width:800px;margin:40px auto">
  <h1>{title}</h1>
  <div class="alert" style="background:#fff3cd;border:1px solid #ffeaa7;padding:16px;margin:20px 0">
    <strong>Your session has expired.</strong>
    Please <a href="./">sign in again</a> to view your invoices.
  </div>
</body>
</html>"""


# ---- App factory ------------------------------------------------------------


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield

    app = FastAPI(title="Iridium Demo Portals", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    def index() -> dict:
        return {
            "service": "demo_portal",
            "portals": {
                "acme_portal": "/acme_portal/",
                "zenith_portal": "/zenith_portal/",
                "globex_portal": "/globex_portal/",
            },
            "credentials_hint": f"{VALID_USERNAME} / {VALID_PASSWORD}",
        }

    @app.get("/{portal_id}/", response_class=HTMLResponse)
    def login_page(portal_id: str) -> HTMLResponse:
        if portal_id not in {"acme_portal", "zenith_portal", "globex_portal"}:
            raise HTTPException(status_code=404, detail="portal not found")
        return HTMLResponse(_login_page(portal_id))

    @app.post("/{portal_id}/login")
    def login(
        portal_id: str,
        username: str = Form(...),
        password: str = Form(...),
    ):
        if portal_id not in {"acme_portal", "zenith_portal", "globex_portal"}:
            raise HTTPException(status_code=404, detail="portal not found")

        if username != VALID_USERNAME or password != VALID_PASSWORD:
            return HTMLResponse(
                _login_page(portal_id, error="Invalid email or password."),
                status_code=401,
            )

        # NB: zenith intentionally accepts the credentials and sets a cookie
        # — the silent failure happens later, on the dashboard render. This
        # mirrors real-world session bugs where auth succeeds but the page
        # renders an expired-state shell.
        cookie_value = _make_session(portal_id, username)
        # Relative redirect — survives whatever path prefix this sub-app is
        # mounted under (e.g. /portal/... in app.py). From .../login the
        # browser resolves "invoices" to .../{portal_id}/invoices.
        resp = RedirectResponse(url="invoices", status_code=303)
        resp.set_cookie(_COOKIE_NAME, cookie_value, httponly=True, samesite="lax")
        return resp

    @app.get("/{portal_id}/invoices", response_class=HTMLResponse)
    def invoices(
        portal_id: str,
        iridium_portal_session: str | None = Cookie(default=None),
    ) -> HTMLResponse:
        if portal_id not in {"acme_portal", "zenith_portal", "globex_portal"}:
            raise HTTPException(status_code=404, detail="portal not found")

        user = _check_session(portal_id, iridium_portal_session)
        if not user:
            return HTMLResponse(
                _login_page(portal_id, error="Please sign in first."),
                status_code=401,
            )

        if portal_id == "zenith_portal":
            return HTMLResponse(_session_expired_page(portal_id))

        invs = PORTAL_INVOICES.get(portal_id, [])
        return HTMLResponse(_invoice_table_page(portal_id, invs))

    @app.get("/{portal_id}/logout")
    def logout(portal_id: str):
        # Relative redirect back to login page — works under any mount prefix.
        resp = RedirectResponse(url="./", status_code=303)
        resp.delete_cookie(_COOKIE_NAME)
        return resp

    return app
