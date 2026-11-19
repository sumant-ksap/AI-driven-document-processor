"""PIN login for when the app is reachable from other devices on the network.

Enabled only when the GATE_PIN environment variable is set (scripts/serve_network.py sets it).
After a correct PIN the browser gets a signed session cookie valid for one shift (12 h).
Changing the PIN or deleting data/session_secret logs every device out.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

COOKIE = "gate_session"
SESSION_SECONDS = 12 * 3600
MAX_FAILURES, FAILURE_WINDOW = 5, 300  # 5 wrong PINs per 5 minutes per device, then locked out
# /api/session only says whether a PIN is needed, so API clients can check before logging in.
OPEN_PATHS = {"/login", "/logout", "/favicon.ico", "/api/session"}
SECRET_FILE = Path(__file__).resolve().parent.parent / "data" / "session_secret"

_failures: dict[str, deque] = defaultdict(deque)


def pin() -> str | None:
    p = os.environ.get("GATE_PIN", "").strip()
    return p or None


def _secret() -> bytes:
    if not SECRET_FILE.exists():
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRET_FILE.write_bytes(secrets.token_bytes(32))
    return SECRET_FILE.read_bytes()


def _sign(issued: str) -> str:
    # The PIN is part of the signature, so changing it invalidates existing sessions.
    return hmac.new(_secret(), f"{issued}|{pin()}".encode(), hashlib.sha256).hexdigest()


def make_token() -> str:
    issued = str(int(time.time()))
    return f"{issued}.{_sign(issued)}"


def valid_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    issued, sig = token.split(".", 1)
    if not issued.isdigit() or time.time() - int(issued) > SESSION_SECONDS:
        return False
    return hmac.compare_digest(sig, _sign(issued))


def _locked(client: str) -> bool:
    q = _failures[client]
    while q and time.time() - q[0] > FAILURE_WINDOW:
        q.popleft()
    return len(q) >= MAX_FAILURES


async def require_pin(request: Request, call_next):
    """HTTP middleware: lets requests through when no PIN is configured or the session is valid."""
    if pin() is None or request.url.path in OPEN_PATHS or valid_token(request.cookies.get(COOKIE)):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Session expired. Enter the PIN again."}, status_code=401)
    return RedirectResponse("/login", status_code=303)


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Gate Check-In – PIN</title>
<style>
 :root {{ --bg:#f5f6f8; --panel:#fff; --ink:#1d2330; --muted:#667085; --line:#e2e5ea; --accent:#2457d6; --err:#c0262d; }}
 * {{ box-sizing: border-box; }}
 body {{ margin:0; min-height:100vh; display:grid; place-items:center; padding:16px; background:var(--bg);
        font:16px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; color:var(--ink); }}
 form {{ width:100%; max-width:340px; background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:24px; }}
 h1 {{ font-size:18px; margin:0 0 4px; }} p {{ color:var(--muted); margin:0 0 16px; font-size:14px; }}
 input {{ width:100%; font-size:28px; letter-spacing:.3em; text-align:center; padding:10px; border:1px solid var(--line); border-radius:8px; }}
 button {{ width:100%; margin-top:14px; padding:12px; border:0; border-radius:8px; background:var(--accent); color:#fff; font:inherit; font-weight:600; }}
 .err {{ color:var(--err); font-size:14px; margin-top:10px; min-height:1.2em; }}
</style></head><body>
<form method="post" action="/login">
  <h1>Gate Check-In Scanner</h1><p>Enter the gate PIN to continue.</p>
  <input name="pin" type="password" inputmode="numeric" autocomplete="current-password" autofocus required aria-label="PIN">
  <button type="submit">Unlock</button>
  <div class="err">{error}</div>
</form></body></html>"""


def login_page(error: str = "") -> HTMLResponse:
    return HTMLResponse(LOGIN_PAGE.format(error=html.escape(error)), headers={"Cache-Control": "no-store"})


async def login_submit(request: Request):
    if pin() is None:
        return RedirectResponse("/", status_code=303)
    client = request.client.host if request.client else "?"
    if _locked(client):
        return login_page("Too many wrong PINs. Wait 5 minutes and try again.")
    form = await request.form()
    entered = str(form.get("pin", "")).strip()
    if not hmac.compare_digest(entered.encode(), pin().encode()):
        _failures[client].append(time.time())
        return login_page("Wrong PIN.")
    _failures.pop(client, None)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, make_token(), max_age=SESSION_SECONDS, httponly=True,
                    secure=request.url.scheme == "https", samesite="strict")
    return resp


def logout() -> RedirectResponse:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp
