from __future__ import annotations

from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.config import settings
from app.db import db_session
from app.models import Participant
from app.scoring import lock_baselines
from app.services import create_account, delete_account
from app.sync import sync_all

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
