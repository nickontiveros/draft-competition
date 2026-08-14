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
        """True P&L vs the effective baseline (uncapped)."""
        return self.current_total - self.baseline_total

    @property
    def busted(self) -> bool:
        """Lost the entire $100 stake — the game loss floor."""
        return self.pnl <= -settings.starting_bankroll

    @property
    def pnl_capped(self) -> float:
        """P&L for display/ranking: only the $100 stake was ever in play, so
        losses cap at -$100 even if the real account fell further."""
        return max(self.pnl, -settings.starting_bankroll)

    @property
    def game_value(self) -> float:
        """The $100-game bankroll: starting stake plus P&L since baseline,
        floored at $0. Independent of real account balances."""
        return max(0.0, settings.starting_bankroll + self.pnl)

    @property
    def pnl_pct(self) -> float:
        base = settings.starting_bankroll
        return 100 * self.pnl_capped / base if base else 0.0

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
                    # Effective baseline: snapshot value plus recorded
                    # deposits/withdrawals, so those never count as P&L.
                    baseline_total=(
                        baseline.total_value + account.baseline_adjustment
                        if baseline
                        else None
                    ),
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
        a.id: (
            db.get(Snapshot, a.baseline_snapshot_id).total_value + a.baseline_adjustment
            if a.baseline_snapshot_id
            else None
        )
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


def _as_utc_ts(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


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
    cost basis. Returns True when the snapshot was changed.

    Ledger exposure only counts for markets that are either open per the
    platform RIGHT NOW, or that the ledger itself closes (sell/settle row)
    after the snapshot — proof the ledger is complete for that market. A
    market the API says is closed with no closing row in the ledger is a
    STALE ledger entry (its settlement fell outside the sync lookback, the
    proceeds already sit in cash); healing from it inflates the baseline and
    makes the player read as a permanent loss."""
    if snap.positions_value:
        return False
    account = db.get(Account, account_id)
    api_open = account.open_markets if account else set()
    rows = db.execute(
        select(Fill.ts, Fill.market_key, Fill.side, Fill.kind, Fill.notional).where(
            Fill.account_id == account_id
        )
    ).all()
    snap_ts = _as_utc_ts(snap.ts)
    closed_later = {
        mk
        for ts, mk, side, _kind, _n in rows
        if mk and side in ("sell", "settle") and _as_utc_ts(ts) >= snap_ts
    }
    allowed = api_open | closed_later
    if not allowed:
        return False
    cost = open_position_cost([r for r in rows if r.market_key in allowed], snap.ts)
    if cost <= 0:
        return False
    snap.positions_value = round(cost, 4)
    snap.total_value = round(snap.cash + cost, 4)
    return True


def rebaseline_account(db: Session, account: Account) -> bool:
    """Stamp the account's latest (healed) snapshot as its baseline and wipe
    any adjustment/flag — the player restarts at exactly $100 from now.
    Returns False when the account has no snapshot yet."""
    latest = latest_snapshot(db, account.id)
    if latest is None:
        return False
    heal_snapshot_positions(db, account.id, latest)
    account.baseline_snapshot_id = latest.id
    account.baseline_adjustment = 0.0
    account.deposit_flag = ""
    return True


def lock_baselines(db: Session) -> int:
    """Rebaseline every account (restart the whole game at $100). Returns
    accounts locked."""
    return sum(1 for account in db.scalars(select(Account)) if rebaseline_account(db, account))


def recent_fills(db: Session, limit: int = 30) -> list[Fill]:
    return list(
        db.scalars(
            select(Fill)
            .options(joinedload(Fill.account).joinedload(Account.participant))
            .order_by(Fill.ts.desc())
            .limit(limit)
        )
    )
