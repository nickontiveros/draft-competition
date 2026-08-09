"""Account creation shared by the admin portal and the /join signup page."""

from __future__ import annotations

import re

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.credentials import seal
from app.db import db_session
from app.models import Account, Participant

_ADDR_RE = re.compile(r"0x[0-9a-fA-F]{40}")

BAD_ADDRESS_MSG = (
    "that doesn't look like a full Polymarket wallet address — it should be 0x "
    "followed by 40 characters. Open your Polymarket profile and tap the "
    "address to copy the complete thing (the shortened '0xAbc…123' display "
    "text won't work)."
)


def normalize_polymarket_address(identifier: str) -> str:
    """Extract a full 0x address from whatever was pasted (address, profile
    URL, text with whitespace). Raises HTTPException(400) if none is found."""
    match = _ADDR_RE.search(identifier)
    if not match:
        raise HTTPException(400, BAD_ADDRESS_MSG)
    return match.group(0).lower()


def create_account(
    name: str, platform: str, identifier: str, private_key_pem: str = ""
) -> int:
    """Create (or reuse) a participant and attach a platform account.
    Raises HTTPException with a human-readable message on any problem."""
    name = name.strip()
    platform = platform.lower().strip()
    identifier = identifier.strip()
    private_key_pem = private_key_pem.strip()

    if not name:
        raise HTTPException(400, "name is required")
    if platform not in ("kalshi", "polymarket"):
        raise HTTPException(400, "platform must be 'kalshi' or 'polymarket'")
    if not identifier:
        raise HTTPException(400, "identifier is required")
    if platform == "kalshi" and not private_key_pem:
        raise HTTPException(400, "Kalshi accounts need the RSA private key PEM")
    if platform == "polymarket":
        identifier = normalize_polymarket_address(identifier)

    if private_key_pem:
        try:
            credentials = seal(private_key_pem)
        except Exception as exc:  # invalid CRED_SECRET (must be a Fernet key)
            raise HTTPException(
                500,
                "CRED_SECRET is misconfigured — it must be a Fernet key, generate one "
                'with: python -c "from cryptography.fernet import Fernet; '
                f'print(Fernet.generate_key().decode())" ({exc})',
            )
    else:
        credentials = ""

    try:
        with db_session() as db:
            participant = db.query(Participant).filter_by(name=name).first()
            if participant is None:
                participant = Participant(name=name)
                db.add(participant)
                db.flush()
            account = Account(
                participant_id=participant.id,
                platform=platform,
                identifier=identifier,
                credentials=credentials,
            )
            db.add(account)
            db.flush()
            return account.id
    except IntegrityError:
        raise HTTPException(
            409, f"a {platform} account with identifier {identifier!r} already exists"
        )


def delete_account(account_id: int) -> bool:
    """Remove an account with its snapshots and fills; drop the participant
    if no accounts remain. Returns False if the account doesn't exist."""
    from app.models import Fill, Snapshot

    with db_session() as db:
        account = db.get(Account, account_id)
        if account is None:
            return False
        participant = account.participant
        account.baseline_snapshot_id = None
        db.flush()
        db.query(Fill).filter_by(account_id=account_id).delete()
        db.query(Snapshot).filter_by(account_id=account_id).delete()
        db.delete(account)
        db.flush()
        if not [a for a in participant.accounts if a.id != account_id]:
            db.delete(participant)
        return True
