from __future__ import annotations

from fastapi import APIRouter, Form, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings
from app.credentials import seal
from app.db import db_session
from app.models import Account, Participant
from app.scoring import lock_baselines
from app.sync import sync_all

router = APIRouter(prefix="/admin")


def _check_token(token: str | None):
    if not settings.admin_token:
        raise HTTPException(503, "ADMIN_TOKEN is not configured on the server")
    if token != settings.admin_token:
        raise HTTPException(403, "bad admin token")


@router.get("", response_class=HTMLResponse)
def admin_page():
    return ADMIN_HTML


@router.post("/participants")
async def add_participant(
    name: str = Form(...),
    platform: str = Form(...),
    identifier: str = Form(...),
    private_key_pem: str = Form(""),
    token: str = Form(None),
    x_admin_token: str | None = Header(None),
):
    _check_token(token or x_admin_token)
    platform = platform.lower().strip()
    if platform not in ("kalshi", "polymarket"):
        raise HTTPException(400, "platform must be 'kalshi' or 'polymarket'")
    if platform == "kalshi" and not private_key_pem.strip():
        raise HTTPException(400, "kalshi accounts need the RSA private key PEM")

    with db_session() as db:
        participant = db.query(Participant).filter_by(name=name.strip()).first()
        if participant is None:
            participant = Participant(name=name.strip())
            db.add(participant)
            db.flush()
        account = Account(
            participant_id=participant.id,
            platform=platform,
            identifier=identifier.strip(),
            credentials=seal(private_key_pem.strip()) if private_key_pem.strip() else "",
        )
        db.add(account)
        db.flush()
        account_id = account.id

    return {"ok": True, "account_id": account_id}


@router.post("/sync")
async def force_sync(token: str = Form(None), x_admin_token: str | None = Header(None)):
    _check_token(token or x_admin_token)
    await sync_all()
    return RedirectResponse("/", status_code=303)


@router.post("/lock-baselines")
async def lock(token: str = Form(None), x_admin_token: str | None = Header(None)):
    _check_token(token or x_admin_token)
    # Sync first so the baseline reflects this moment, not the last poll.
    await sync_all()
    with db_session() as db:
        count = lock_baselines(db)
    return {"ok": True, "accounts_locked": count}


ADMIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Tracker admin</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;max-width:560px;margin:2rem auto;padding:0 1rem;background:#f9f9f7;color:#0b0b0b}
 @media (prefers-color-scheme: dark){body{background:#0d0d0d;color:#fff} input,textarea,select{background:#1a1a19;color:#fff;border-color:#383835}}
 fieldset{border:1px solid #c3c2b7;border-radius:8px;margin-bottom:1.5rem;padding:1rem}
 label{display:block;margin:.6rem 0 .2rem;font-size:.9rem}
 input,textarea,select{width:100%;padding:.45rem;border:1px solid #c3c2b7;border-radius:6px;box-sizing:border-box}
 button{margin-top:.8rem;padding:.5rem 1rem;border:none;border-radius:6px;background:#2a78d6;color:#fff;font-size:1rem;cursor:pointer}
</style></head><body>
<h1>Tracker admin</h1>
<form method="post" action="/admin/participants">
 <fieldset><legend>Add participant account</legend>
  <label>Admin token</label><input name="token" type="password" required>
  <label>Participant name</label><input name="name" required>
  <label>Platform</label>
  <select name="platform"><option>polymarket</option><option>kalshi</option></select>
  <label>Identifier (Polymarket wallet address / Kalshi API key ID)</label>
  <input name="identifier" required>
  <label>Kalshi RSA private key PEM (kalshi only)</label>
  <textarea name="private_key_pem" rows="4" placeholder="-----BEGIN RSA PRIVATE KEY-----"></textarea>
  <button>Add account</button>
 </fieldset>
</form>
<form method="post" action="/admin/sync">
 <fieldset><legend>Force sync now</legend>
  <label>Admin token</label><input name="token" type="password" required>
  <button>Sync all accounts</button>
 </fieldset>
</form>
<form method="post" action="/admin/lock-baselines">
 <fieldset><legend>Lock baselines (run at competition start)</legend>
  <label>Admin token</label><input name="token" type="password" required>
  <button>Snapshot &amp; lock baselines</button>
 </fieldset>
</form>
</body></html>"""
