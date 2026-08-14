from __future__ import annotations

from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.config import settings
from app.db import db_session
from app.models import Account, Participant
from app.scoring import lock_baselines, rebaseline_account
from app.services import create_account, delete_account
from app.sync import sync_account, sync_all

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")


def _check_token(token: str | None):
    if not settings.admin_token:
        raise HTTPException(503, "ADMIN_TOKEN is not configured on the server")
    if token != settings.admin_token:
        raise HTTPException(403, "bad admin token")


@router.get("", response_class=HTMLResponse)
def admin_page(request: Request, token: str | None = None):
    """Forms always render; the participant list appears once ?token= is valid."""
    participants = []
    authed = bool(settings.admin_token) and token == settings.admin_token
    if authed:
        with db_session() as db:
            participants = list(
                db.scalars(
                    select(Participant)
                    .options(joinedload(Participant.accounts))
                    .order_by(Participant.name)
                ).unique()
            )
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"participants": participants, "authed": authed, "token": token or ""},
    )


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
    account_id = create_account(name, platform, identifier, private_key_pem)
    return {"ok": True, "account_id": account_id}


@router.post("/accounts/{account_id}/delete")
async def remove_account(
    account_id: int,
    token: str = Form(None),
    x_admin_token: str | None = Header(None),
):
    _check_token(token or x_admin_token)
    if not delete_account(account_id):
        raise HTTPException(404, "no such account")
    return RedirectResponse(f"/admin?token={token or x_admin_token}", status_code=303)


@router.post("/accounts/{account_id}/rebaseline")
async def rebaseline(
    account_id: int,
    token: str = Form(None),
    x_admin_token: str | None = Header(None),
):
    """Restart one player at $100 from this moment: sync a fresh snapshot,
    stamp it as the baseline, wipe adjustment and deposit flag."""
    _check_token(token or x_admin_token)
    with db_session() as db:
        if db.get(Account, account_id) is None:
            raise HTTPException(404, "no such account")
    await sync_account(account_id)  # baseline should reflect right now
    with db_session() as db:
        account = db.get(Account, account_id)
        if account is None:
            raise HTTPException(404, "no such account")
        if not rebaseline_account(db, account):
            raise HTTPException(
                409, "account has no snapshot yet — check its sync error and retry"
            )
    return RedirectResponse(f"/admin?token={token or x_admin_token}", status_code=303)


@router.post("/accounts/{account_id}/adjust")
async def adjust_baseline(
    account_id: int,
    amount: float = Form(...),
    token: str = Form(None),
    x_admin_token: str | None = Header(None),
):
    """Record a mid-game deposit (+) or withdrawal (-): shifts the effective
    baseline by that amount so it never counts as P&L, preserving the
    player's real trading results."""
    _check_token(token or x_admin_token)
    if amount == 0:
        raise HTTPException(400, "amount must be non-zero (positive = deposit, negative = withdrawal)")
    with db_session() as db:
        account = db.get(Account, account_id)
        if account is None:
            raise HTTPException(404, "no such account")
        account.baseline_adjustment += amount
        account.deposit_flag = ""
    return RedirectResponse(f"/admin?token={token or x_admin_token}", status_code=303)


@router.post("/accounts/{account_id}/clear-flag")
async def clear_flag(
    account_id: int,
    token: str = Form(None),
    x_admin_token: str | None = Header(None),
):
    _check_token(token or x_admin_token)
    with db_session() as db:
        account = db.get(Account, account_id)
        if account is None:
            raise HTTPException(404, "no such account")
        account.deposit_flag = ""
    return RedirectResponse(f"/admin?token={token or x_admin_token}", status_code=303)


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
