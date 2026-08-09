from __future__ import annotations

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.services import create_account, normalize_polymarket_address
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


async def _polymarket_preflight(identifier: str) -> str | None:
    """Ask Polymarket about the address before accepting it. Returns an error
    message only when the API definitively rejects the address (400) — network
    hiccups never block a signup."""
    if settings.mock_connectors:
        return None
    from app.connectors.polymarket import PolymarketConnector

    try:
        await PolymarketConnector(identifier)._positions_value()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400:
            return (
                "Polymarket doesn't recognize that wallet address — double-check "
                "you copied the full 0x… address from your profile page."
            )
    except Exception:  # noqa: BLE001 - preflight is best-effort
        pass
    return None


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
        if platform.lower().strip() == "polymarket":
            identifier = normalize_polymarket_address(identifier)
            error = await _polymarket_preflight(identifier)
            if error:
                raise HTTPException(400, error)
        account_id = create_account(name, platform, identifier, private_key_pem)
    except HTTPException as exc:
        return RedirectResponse(f"/join?error={exc.detail}", status_code=303)
    # First sync now: stamps the $100 baseline and puts them on the board today.
    await sync_account(account_id)
    return RedirectResponse(f"/?welcome={name.strip()}", status_code=303)
