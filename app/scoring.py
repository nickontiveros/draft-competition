from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

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
    def pnl_pct(self) -> float:
        base = self.baseline_total or settings.starting_bankroll
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


def lock_baselines(db: Session) -> int:
    """Stamp each account's latest snapshot as its baseline. Returns accounts locked."""
    count = 0
    for account in db.scalars(select(Account)):
        latest = latest_snapshot(db, account.id)
        if latest is not None:
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
