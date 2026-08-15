from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.config import settings
from app.db import db_session
from app.models import Account, Participant
from app.scoring import backdate_baseline, lock_baselines, rebaseline_account
from app.services import create_account, delete_account
from app.sync import FIRST_SYNC_LOOKBACK, backfill_history, sync_account, sync_all

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
        {
            "participants": participants,
            "authed": authed,
            "token": token or "",
            "version": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "unknown")[:7],
        },
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


@router.get("/accounts/{account_id}/history")
def account_history(account_id: int, token: str | None = None):
    """Diagnostic dump: baseline, latest valuation trace, recent snapshots.
    Contains no credentials — dollar values, timestamps, and market tickers."""
    _check_token(token)
    from app.models import Snapshot

    with db_session() as db:
        account = db.get(Account, account_id)
        if account is None:
            raise HTTPException(404, "no such account")
        baseline = (
            db.get(Snapshot, account.baseline_snapshot_id)
            if account.baseline_snapshot_id
            else None
        )
        snapshots = db.scalars(
            select(Snapshot)
            .where(Snapshot.account_id == account_id)
            .order_by(Snapshot.ts.desc())
            .limit(50)
        ).all()
        try:
            valuation = json.loads(account.valuation_json or "{}")
        except ValueError:
            valuation = {}
        return {
            "account_id": account.id,
            "participant": account.participant.name,
            "platform": account.platform,
            "baseline": (
                {
                    "snapshot_id": baseline.id,
                    "ts": baseline.ts.isoformat(),
                    "cash": baseline.cash,
                    "positions_value": baseline.positions_value,
                    "reserved": baseline.reserved,
                    "total_value": baseline.total_value,
                }
                if baseline
                else None
            ),
            "baseline_adjustment": account.baseline_adjustment,
            "deposit_flag": account.deposit_flag,
            "last_sync_error": account.last_sync_error,
            "last_sync_note": account.last_sync_note,
            "latest_valuation": valuation,
            "pending_orders": account.pending_orders,
            "open_markets": sorted(account.open_markets),
            "snapshots": [
                {
                    "ts": s.ts.isoformat(),
                    "cash": s.cash,
                    "positions_value": s.positions_value,
                    "reserved": s.reserved,
                    "total_value": s.total_value,
                    "is_baseline": s.id == account.baseline_snapshot_id,
                }
                for s in snapshots
            ],
        }


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


@router.post("/accounts/{account_id}/backdate")
async def backdate(
    account_id: int,
    to: str = Form(...),
    token: str = Form(None),
    x_admin_token: str | None = Header(None),
):
    """Move a late joiner's baseline back to a past moment: sync fresh, then
    backfill the ledger to that moment and reconstruct a synthetic baseline
    from the cash-flow replay. Their betting counts from that moment on."""
    _check_token(token or x_admin_token)
    try:
        to_ts = datetime.fromisoformat(to)
    except ValueError:
        raise HTTPException(400, f"can't parse {to!r} as a date/time")
    if to_ts.tzinfo is None:
        to_ts = to_ts.replace(tzinfo=timezone.utc)  # form input is UTC
    now = datetime.now(timezone.utc)
    if to_ts >= now:
        raise HTTPException(400, "backdate target must be in the past")
    if to_ts < now - FIRST_SYNC_LOOKBACK:
        raise HTTPException(
            400,
            f"backdate target can be at most {FIRST_SYNC_LOOKBACK.days} days back — "
            "the platforms' history fetches (and therefore the replay) can't be "
            "trusted beyond that",
        )
    with db_session() as db:
        if db.get(Account, account_id) is None:
            raise HTTPException(404, "no such account")

    await sync_account(account_id)  # fresh snapshot to replay from
    try:
        await backfill_history(account_id, since=to_ts)
    except Exception as exc:  # noqa: BLE001 - incomplete ledger => no backdate
        raise HTTPException(
            502, f"couldn't backfill trade history to that date, not backdating: {exc}"
        )

    with db_session() as db:
        account = db.get(Account, account_id)
        if account is None or backdate_baseline(db, account, to_ts) is None:
            raise HTTPException(
                409, "account has no snapshot to replay from — check its sync error"
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
