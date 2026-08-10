from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.connectors.base import NormalizedFill
from app.credentials import unseal
from app.db import db_session
from app.models import Account, Fill, MarketMeta, Snapshot
from app.scoring import heal_snapshot_positions

logger = logging.getLogger(__name__)

# Cash increases above this (in dollars) with no explanatory sells/settlements
# get the account flagged as a possible mid-competition deposit.
DEPOSIT_FLAG_THRESHOLD = 1.0

# Rolling fill lookback; the first sync of an account reaches further back so
# recently placed bets show up in the ledger (tagged pre-game by stats).
LOOKBACK = timedelta(days=2)
FIRST_SYNC_LOOKBACK = timedelta(days=30)


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
    """Sync one account: snapshot cash/positions, upsert fills + settlements,
    auto-stamp the baseline, and enrich market metadata.

    Errors are recorded on the account, never raised — one broken account
    must not block the rest of the leaderboard.
    """
    with db_session() as db:
        account = db.get(Account, account_id)
        platform = account.platform
        connector = build_connector(account)
        prev = _latest_snapshot(db, account_id)

    since = datetime.now(timezone.utc) - (LOOKBACK if prev else FIRST_SYNC_LOOKBACK)
    try:
        state = await connector.fetch_state()
        fills = await connector.fetch_fills(since=since)
        settlements = await connector.fetch_settlements(since=since)
    except Exception as exc:  # noqa: BLE001 - isolate per-account failures
        logger.warning("sync failed for account %s: %s", account_id, exc)
        with db_session() as db:
            account = db.get(Account, account_id)
            account.last_sync_error = _friendly_error(platform, exc)
        return

    events = fills + settlements
    with db_session() as db:
        account = db.get(Account, account_id)

        snapshot = Snapshot(
            account_id=account_id,
            cash=state.cash,
            positions_value=state.positions_value,
            total_value=state.total,
        )
        db.add(snapshot)
        db.flush()

        new_fills = 0
        for f in events:
            exists = db.scalars(
                select(Fill.id).where(
                    Fill.platform == platform, Fill.external_id == f.external_id
                )
            ).first()
            if exists:
                continue
            db.add(
                Fill(
                    account_id=account_id,
                    platform=platform,
                    external_id=f.external_id,
                    ts=f.ts,
                    market_title=f.market_title,
                    outcome=f.outcome,
                    side=f.side,
                    size=f.size,
                    price=f.price,
                    market_key=f.market_key,
                    notional=f.notional,
                    kind=f.kind,
                    raw_json=json.dumps(f.raw),
                )
            )
            new_fills += 1

        # The $100 game measures P&L from each account's first snapshot.
        # Stamped after the fill insert above so heal_snapshot_positions can
        # see the full ledger: a candidate snapshot claiming $0 in positions
        # while fills show open exposure gets the cost basis patched in,
        # otherwise every straddling bet's full payout would count as P&L.
        if account.baseline_snapshot_id is None:
            earliest = db.scalars(
                select(Snapshot)
                .where(Snapshot.account_id == account_id)
                .order_by(Snapshot.ts)
                .limit(1)
            ).first()
            candidate = earliest or snapshot
            if heal_snapshot_positions(db, account_id, candidate):
                logger.warning(
                    "account %s: baseline snapshot claimed $0 positions but the "
                    "ledger shows open exposure — patched in cost basis",
                    account_id,
                )
            account.baseline_snapshot_id = candidate.id

        if prev is not None:
            _check_deposit(account, prev, state.cash, events)

        account.open_markets_json = json.dumps(sorted(state.open_market_keys))
        account.last_sync_at = datetime.now(timezone.utc)
        account.last_sync_error = ""
        logger.info(
            "synced account %s (%s): total=%.2f, %d new fills",
            account_id,
            platform,
            state.total,
            new_fills,
        )

    await _enrich_market_meta(connector, platform, events)


def _friendly_error(platform: str, exc: Exception) -> str:
    msg = str(exc)
    if platform == "polymarket" and "400" in msg:
        return (
            "Polymarket rejected this wallet address (400) — remove this account "
            "and re-add it with the full 0x… address from the profile page"
        )
    if platform == "kalshi" and ("401" in msg or "403" in msg):
        return (
            "Kalshi rejected the API key (auth failed) — check the key ID and "
            "private key, and that the key has read scopes"
        )
    return msg[:500]


def _latest_snapshot(db, account_id: int) -> Snapshot | None:
    return db.scalars(
        select(Snapshot)
        .where(Snapshot.account_id == account_id)
        .order_by(Snapshot.ts.desc())
        .limit(1)
    ).first()


async def _enrich_market_meta(
    connector, platform: str, events: list[NormalizedFill]
) -> None:
    """Fetch title/category/yes_sub_title for markets we haven't seen, then
    backfill those fields onto stored fills. Markets whose cached category
    never resolved past ""/"Other" are retried each sync, so a fixed
    connector heals old data. Best-effort: failures just leave fills with
    their raw ticker/title until a later sync."""
    keys = {e.market_key for e in events if e.market_key}
    with db_session() as db:
        # Older fills outside the sync lookback whose category never resolved.
        stale_fill_rows = db.execute(
            select(Fill.market_key, Fill.raw_json).where(
                Fill.platform == platform,
                Fill.market_key != "",
                Fill.category.in_(("", "Other")),
            )
        ).all()
        keys |= {mk for mk, _ in stale_fill_rows}
        if not keys:
            return
        known = set(
            db.scalars(
                select(MarketMeta.market_key).where(
                    MarketMeta.platform == platform,
                    MarketMeta.market_key.in_(keys),
                    MarketMeta.category.notin_(("", "Other")),
                )
            )
        )
    unknown = sorted(keys - known)
    if unknown:
        hints: dict[str, dict] = {}
        for mk, raw_json in stale_fill_rows:
            if mk in unknown and raw_json:
                try:
                    hints[mk] = json.loads(raw_json)
                except ValueError:
                    pass
        hints |= {e.market_key: e.raw for e in events if e.market_key in unknown}
        try:
            metas = await connector.fetch_market_meta(unknown, hints=hints)
        except Exception as exc:  # noqa: BLE001
            logger.warning("market meta fetch failed (%s): %s", platform, exc)
            metas = {}
        if metas:
            with db_session() as db:
                existing = {
                    m.market_key: m
                    for m in db.scalars(
                        select(MarketMeta).where(
                            MarketMeta.platform == platform,
                            MarketMeta.market_key.in_(list(metas)),
                        )
                    )
                }
                for key, info in metas.items():
                    row = existing.get(key)
                    if row is None:
                        db.add(
                            MarketMeta(
                                platform=platform,
                                market_key=key,
                                title=info.title,
                                category=info.category,
                                yes_sub_title=info.yes_sub_title,
                                raw_json=json.dumps(info.raw),
                            )
                        )
                    else:
                        row.title = info.title or row.title
                        row.category = info.category
                        row.yes_sub_title = info.yes_sub_title or row.yes_sub_title
                        row.raw_json = json.dumps(info.raw)

    # Backfill readable titles/categories onto any fills still missing them.
    with db_session() as db:
        metas_db = db.scalars(
            select(MarketMeta).where(
                MarketMeta.platform == platform, MarketMeta.market_key.in_(keys)
            )
        ).all()
        by_key = {m.market_key: m for m in metas_db}
        stale = db.scalars(
            select(Fill).where(
                Fill.platform == platform,
                Fill.market_key.in_(by_key),
                Fill.category.in_(("", "Other")),
            )
        ).all()
        for fill in stale:
            meta = by_key[fill.market_key]
            fill.category = meta.category or "Other"
            if meta.title:
                fill.market_title = meta.title


def _check_deposit(
    account: Account, prev: Snapshot, new_cash: float, events: list[NormalizedFill]
) -> None:
    """Flag cash jumps that selling/settlement can't explain (rule: no mid-window deposits)."""
    cash_increase = new_cash - prev.cash
    if cash_increase <= DEPOSIT_FLAG_THRESHOLD:
        return
    prev_ts = prev.ts if prev.ts.tzinfo else prev.ts.replace(tzinfo=timezone.utc)
    explained = sum(
        e.notional for e in events if e.side in ("sell", "settle") and e.ts >= prev_ts
    )
    # Allow generous slack: any open position could also have settled at $1.
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
