from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.services import create_account
from app.sync import sync_account

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _check_passphrase(passphrase: str):
    if not settings.signup_passphrase:
        raise HTTPException(
            503, "signup is disabled — SIGNUP_PASSPHRASE is not configured on the server"
        )
    if passphrase != settings.signup_passphrase:
        raise HTTPException(403, "wrong passphrase — ask the organizer for it")


@router.get("/join", response_class=HTMLResponse)
def join_page(request: Request, error: str | None = None):
    return templates.TemplateResponse(
        request,
        "join.html",
        {"error": error, "enabled": bool(settings.signup_passphrase)},
    )


@router.post("/join")
async def join(
    passphrase: str = Form(...),
    name: str = Form(...),
    platform: str = Form(...),
    identifier: str = Form(...),
    private_key_pem: str = Form(""),
):
    _check_passphrase(passphrase)
    try:
        account_id = create_account(name, platform, identifier, private_key_pem)
    except HTTPException as exc:
        return RedirectResponse(f"/join?error={exc.detail}", status_code=303)
    # First sync now: stamps the $100 baseline and puts them on the board today.
    await sync_account(account_id)
    return RedirectResponse(f"/?welcome={name.strip()}", status_code=303)
