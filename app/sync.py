from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.credentials import unseal
from app.db import db_session
from app.models import Account, Fill, Snapshot

logger = logging.getLogger(__name__)

# Cash increases above this (in dollars) with no explanatory sells/redeems
# get the account flagged as a possible mid-competition deposit.
DEPOSIT_FLAG_THRESHOLD = 1.0


def build_connector(account: Account):
    if settings.mock_connectors:
        from app.connectors.mock import MockConnector

        return MockConnector(seed_key=f"{account.platform}:{account.identifier}")
    if account.platform == "polymarket":
        from app.connectors.polymarket import PolymarketConnector

        return PolymarketConnector(wallet_address=account.identifier)
    if account.platform == "kalshi":
        from app.connectors.kalshi import KalshiConnector

        return KalshiConnector(
            api_key_id=account.identifier,
            private_key_pem=unseal(account.credentials),
        )
    raise ValueError(f"unknown platform {account.platform!r}")


async def sync_account(account_id: int) -> None:
    """Sync one account: snapshot cash/positions and upsert recent fills.

    Errors are recorded on the account, never raised — one broken account
    must not block the rest of the leaderboard.
    """
    with db_session() as db:
        account = db.get(Account, account_id)
        connector = build_connector(account)

    try:
        state = await connector.fetch_state()
        since = datetime.now(timezone.utc) - timedelta(days=2)
        fills = await connector.fetch_fills(since=since)
    except Exception as exc:  # noqa: BLE001 - isolate per-account failures
        logger.warning("sync failed for account %s: %s", account_id, exc)
        with db_session() as db:
            account = db.get(Account, account_id)
            account.last_sync_error = str(exc)[:500]
        return

    with db_session() as db:
        account = db.get(Account, account_id)

        prev = db.scalars(
            select(Snapshot)
            .where(Snapshot.account_id == account_id)
            .order_by(Snapshot.ts.desc())
            .limit(1)
        ).first()

        snapshot = Snapshot(
            account_id=account_id,
            cash=state.cash,
            positions_value=state.positions_value,
            total_value=state.total,
        )
        db.add(snapshot)

        new_fills = 0
        for f in fills:
            exists = db.scalars(
                select(Fill.id).where(
                    Fill.platform == account.platform, Fill.external_id == f.external_id
                )
            ).first()
            if exists:
                continue
            db.add(
                Fill(
                    account_id=account_id,
                    platform=account.platform,
                    external_id=f.external_id,
                    ts=f.ts,
                    market_title=f.market_title,
                    outcome=f.outcome,
                    side=f.side,
                    size=f.size,
                    price=f.price,
                    raw_json=json.dumps(f.raw),
                )
            )
            new_fills += 1

        if prev is not None:
            _check_deposit(account, prev, state.cash, fills)

        account.last_sync_at = datetime.now(timezone.utc)
        account.last_sync_error = ""
        logger.info(
            "synced account %s (%s): total=%.2f, %d new fills",
            account_id,
            account.platform,
            state.total,
            new_fills,
        )


def _check_deposit(account: Account, prev: Snapshot, new_cash: float, fills) -> None:
    """Flag cash jumps that selling/settlement can't explain (rule: no mid-window deposits)."""
    cash_increase = new_cash - prev.cash
    if cash_increase <= DEPOSIT_FLAG_THRESHOLD:
        return
    prev_ts = prev.ts if prev.ts.tzinfo else prev.ts.replace(tzinfo=timezone.utc)
    explained = sum(
        f.size * f.price for f in fills if f.side == "sell" and f.ts >= prev_ts
    )
    # Settlements/redeems pay out up to $1/contract; allow generous slack before flagging.
    if cash_increase > explained + prev.positions_value + DEPOSIT_FLAG_THRESHOLD:
        account.deposit_flag = (
            f"cash +${cash_increase:.2f} at {datetime.now(timezone.utc):%m-%d %H:%M} UTC "
            "not explained by sells/settlements"
        )


async def sync_all() -> None:
    with db_session() as db:
        account_ids = list(db.scalars(select(Account.id)))
    for account_id in account_ids:
        await sync_account(account_id)


def run_sync_all() -> None:
    """Sync entry point for APScheduler (runs in a worker thread)."""
    asyncio.run(sync_all())
