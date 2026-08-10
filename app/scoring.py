from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.models import Account, Fill, Participant, Snapshot


@dataclass
class AccountStanding:
    platform: str
    identifier: str
    current_total: float | None
    baseline_total: float | None
    last_sync_at: datetime | None
    last_sync_error: str
    deposit_flag: str


@dataclass
class Standing:
    participant_id: int
    name: str
    accounts: list[AccountStanding] = field(default_factory=list)
    history: list[tuple[datetime, float]] = field(default_factory=list)

    @property
    def current_total(self) -> float:
        return sum(a.current_total or 0.0 for a in self.accounts)

    @property
    def baseline_total(self) -> float:
        return sum(a.baseline_total or 0.0 for a in self.accounts)

    @property
    def pnl(self) -> float:
        return self.current_total - self.baseline_total

    @property
    def game_value(self) -> float:
        """The $100-game bankroll: starting stake plus P&L since baseline.
        Independent of how much real money sits in the underlying accounts."""
        return settings.starting_bankroll + self.pnl

    @property
    def pnl_pct(self) -> float:
        base = settings.starting_bankroll
        return 100 * self.pnl / base if base else 0.0

    @property
    def flags(self) -> list[str]:
        out = []
        for a in self.accounts:
            if a.deposit_flag:
                out.append(f"{a.platform}: {a.deposit_flag}")
            if a.last_sync_error:
                out.append(f"{a.platform}: sync error — {a.last_sync_error}")
        return out


def latest_snapshot(db: Session, account_id: int) -> Snapshot | None:
    return db.scalars(
        select(Snapshot)
        .where(Snapshot.account_id == account_id)
        .order_by(Snapshot.ts.desc())
        .limit(1)
    ).first()


def compute_standings(db: Session) -> list[Standing]:
    participants = db.scalars(
        select(Participant).options(joinedload(Participant.accounts)).order_by(Participant.name)
    ).unique()

    standings = []
    for p in participants:
        s = Standing(participant_id=p.id, name=p.name)
        for account in p.accounts:
            latest = latest_snapshot(db, account.id)
            baseline = (
                db.get(Snapshot, account.baseline_snapshot_id)
                if account.baseline_snapshot_id
                else None
            )
            s.accounts.append(
                AccountStanding(
                    platform=account.platform,
                    identifier=account.identifier,
                    current_total=latest.total_value if latest else None,
                    baseline_total=baseline.total_value if baseline else None,
                    last_sync_at=account.last_sync_at,
                    last_sync_error=account.last_sync_error,
                    deposit_flag=account.deposit_flag,
                )
            )
        s.history = participant_history(db, p)
        standings.append(s)

    standings.sort(key=lambda s: s.pnl, reverse=True)
    return standings


def participant_history(db: Session, participant: Participant, points: int = 60) -> list[tuple[datetime, float]]:
    """Summed P&L over time across the participant's accounts (for sparklines).

    Snapshots across accounts land at nearly the same instant (one sync run),
    so we bucket by sync run: sort all snapshots, group within 90 seconds.
    """
    account_ids = [a.id for a in participant.accounts]
    if not account_ids:
        return []
    baselines = {
        a.id: (db.get(Snapshot, a.baseline_snapshot_id).total_value if a.baseline_snapshot_id else None)
        for a in participant.accounts
    }
    rows = db.scalars(
        select(Snapshot).where(Snapshot.account_id.in_(account_ids)).order_by(Snapshot.ts)
    ).all()

    buckets: list[tuple[datetime, dict[int, float]]] = []
    for snap in rows:
        if buckets and (snap.ts - buckets[-1][0]).total_seconds() < 90:
            buckets[-1][1][snap.account_id] = snap.total_value
        else:
            buckets.append((snap.ts, {snap.account_id: snap.total_value}))

    history = []
    last_seen: dict[int, float] = {}
    for ts, values in buckets:
        last_seen.update(values)
        if len(last_seen) < len(account_ids):
            continue  # wait until every account has at least one snapshot
        baseline_sum = sum(
            baselines[aid] if baselines[aid] is not None else last_seen[aid]
            for aid in account_ids
        )
        history.append((ts, sum(last_seen.values()) - baseline_sum))
    return history[-points:]


def open_position_cost(fills: list[tuple], at_ts: datetime) -> float:
    """Cost basis of positions still open at `at_ts`, inferred from the fill
    ledger: per market, dollars in (buys) minus dollars back (sells and
    settlements) before that moment; whatever exposure remains was an open
    position. Used to sanity-check baseline snapshots — a snapshot claiming
    $0 in positions while the ledger shows open exposure was recorded while
    the connector couldn't price positions (e.g. Kalshi's cents-fields
    removal), and a baseline stamped from it inflates P&L by the payout of
    every straddling bet.

    `fills` are (ts, market_key, side, kind, notional) tuples so callers can
    feed either ORM rows or raw SQL rows.
    """
    at_ts = at_ts if at_ts.tzinfo else at_ts.replace(tzinfo=timezone.utc)
    remaining: dict[str, float] = {}
    for ts, market_key, side, kind, notional in fills:
        if not market_key:
            continue
        ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        if ts >= at_ts:
            continue
        if kind == "trade" and side == "buy":
            remaining[market_key] = remaining.get(market_key, 0.0) + (notional or 0.0)
        elif side in ("sell", "settle"):
            remaining[market_key] = remaining.get(market_key, 0.0) - (notional or 0.0)
    return sum(v for v in remaining.values() if v > 0)


def heal_snapshot_positions(db: Session, account_id: int, snap: Snapshot) -> bool:
    """If a snapshot about to become a baseline claims zero position value
    while the fill ledger shows open exposure at its timestamp, patch in the
    cost basis. Returns True when the snapshot was changed."""
    if snap.positions_value:
        return False
    rows = db.execute(
        select(Fill.ts, Fill.market_key, Fill.side, Fill.kind, Fill.notional).where(
            Fill.account_id == account_id
        )
    ).all()
    cost = open_position_cost(rows, snap.ts)
    if cost <= 0:
        return False
    snap.positions_value = round(cost, 4)
    snap.total_value = round(snap.cash + cost, 4)
    return True


def lock_baselines(db: Session) -> int:
    """Stamp each account's latest snapshot as its baseline. Returns accounts locked."""
    count = 0
    for account in db.scalars(select(Account)):
        latest = latest_snapshot(db, account.id)
        if latest is not None:
            heal_snapshot_positions(db, account.id, latest)
            account.baseline_snapshot_id = latest.id
            count += 1
    return count


def recent_fills(db: Session, limit: int = 30) -> list[Fill]:
    return list(
        db.scalars(
            select(Fill)
            .options(joinedload(Fill.account).joinedload(Account.participant))
            .order_by(Fill.ts.desc())
            .limit(limit)
        )
    )
