"""Per-player bet ledger and derived stats.

Fills (buys/sells) and settlements are grouped by market into "bets". A bet is:
  - open     — the account still holds a position in that market
  - won/lost — position closed; won iff dollars returned > dollars wagered
  - pre-game — bought before the account's baseline; shown but excluded from
               stats (its value is already inside the baseline snapshot)
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.links import bet_url
from app.models import Account, Fill, MarketMeta, Participant, Snapshot


@dataclass
class BetEntry:
    market_key: str
    platform: str
    title: str
    category: str
    outcome: str
    yes_sub_title: str
    contracts: float  # contracts bought
    avg_price: float  # dollars per contract on buys
    wagered: float
    returned: float
    status: str  # "open" | "won" | "lost" | "pre-game" | "pending"
    last_ts: datetime
    url: str | None = None  # market page on the source platform

    @property
    def pnl(self) -> float | None:
        if self.status in ("won", "lost"):
            return self.returned - self.wagered
        return None

    @property
    def outcome_label(self) -> str:
        """Prefer the human meaning of the position (team names etc.)."""
        sub = self.yes_sub_title
        if sub and sub != self.outcome:
            if self.outcome == "Yes":
                return sub
            if self.outcome == "No":
                return f"No — {sub}"
        return self.outcome


@dataclass
class CategoryStat:
    name: str
    wagered: float = 0.0
    bets: int = 0
    wins: int = 0
    losses: int = 0


@dataclass
class PlayerStats:
    entries: list[BetEntry] = field(default_factory=list)
    total_wagered: float = 0.0
    bets_placed: int = 0
    wins: int = 0
    losses: int = 0
    biggest_win: BetEntry | None = None
    biggest_loss: BetEntry | None = None
    categories: list[CategoryStat] = field(default_factory=list)

    @property
    def win_rate(self) -> float | None:
        decided = self.wins + self.losses
        return 100 * self.wins / decided if decided else None


def _as_utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _meta_raw(meta: MarketMeta | None) -> dict | None:
    if meta is None or not meta.raw_json:
        return None
    try:
        return json.loads(meta.raw_json)
    except ValueError:
        return None


def compute_player_stats(db: Session, participant: Participant) -> PlayerStats:
    stats = PlayerStats()
    all_entries: list[BetEntry] = []

    for account in participant.accounts:
        baseline_ts = None
        if account.baseline_snapshot_id:
            baseline = db.get(Snapshot, account.baseline_snapshot_id)
            if baseline:
                baseline_ts = _as_utc(baseline.ts)

        fills = db.scalars(
            select(Fill).where(Fill.account_id == account.id).order_by(Fill.ts)
        ).all()
        groups: dict[str, list[Fill]] = {}
        for f in fills:
            groups.setdefault(f.market_key or f.external_id, []).append(f)

        pending = account.pending_orders
        keys = [k for k in groups if k] + [
            o["market_key"] for o in pending if o.get("market_key")
        ]
        metas = {
            m.market_key: m
            for m in db.scalars(
                select(MarketMeta).where(
                    MarketMeta.platform == account.platform,
                    MarketMeta.market_key.in_(keys),
                )
            )
        }
        open_markets = account.open_markets

        for key, group in groups.items():
            buys = [f for f in group if f.kind == "trade" and f.side == "buy"]
            inflows = [f for f in group if f.side in ("sell", "settle")]
            wagered = sum(f.notional for f in buys)
            returned = sum(f.notional for f in inflows)
            contracts = sum(f.size for f in buys)
            meta = metas.get(key)

            first_buy_ts = _as_utc(buys[0].ts) if buys else None
            pre_game = (
                not buys
                or (baseline_ts is not None and first_buy_ts < baseline_ts)
            )
            if pre_game:
                status = "pre-game"
            elif key in open_markets:
                status = "open"
            else:
                status = "won" if returned > wagered else "lost"

            outcomes = Counter(f.outcome for f in buys if f.outcome)
            outcome = outcomes.most_common(1)[0][0] if outcomes else group[-1].outcome
            title = next((f.market_title for f in group if f.market_title), key)

            all_entries.append(
                BetEntry(
                    market_key=key,
                    platform=account.platform,
                    title=(meta.title if meta and meta.title else title),
                    category=(
                        meta.category
                        if meta and meta.category
                        else next((f.category for f in group if f.category), "Other")
                    ),
                    outcome=outcome,
                    yes_sub_title=meta.yes_sub_title if meta else "",
                    contracts=contracts,
                    avg_price=(wagered / contracts) if contracts else 0.0,
                    wagered=wagered,
                    returned=returned,
                    status=status,
                    last_ts=_as_utc(group[-1].ts),
                    url=bet_url(
                        account.platform,
                        group[0].market_key,  # not `key`: keyless groups fall back to external_id
                        (buys[0] if buys else group[-1]).raw,
                        _meta_raw(meta),
                    ),
                )
            )

        # Resting (unfilled) orders — bets placed but not yet matched. Shown
        # in the ledger so "where's my bet?" has an answer; excluded from
        # stats since no money is at risk until the order fills.
        for od in pending:
            key = od.get("market_key", "")
            meta = metas.get(key)
            try:
                ts = datetime.fromisoformat(od.get("ts", ""))
            except ValueError:
                ts = datetime.now(timezone.utc)
            all_entries.append(
                BetEntry(
                    market_key=key,
                    platform=account.platform,
                    title=(meta.title if meta and meta.title else key),
                    category=(meta.category if meta and meta.category else "Other"),
                    outcome=od.get("outcome", ""),
                    yes_sub_title=meta.yes_sub_title if meta else "",
                    contracts=od.get("size", 0.0),
                    avg_price=od.get("price", 0.0),
                    wagered=od.get("reserved", 0.0),
                    returned=0.0,
                    status="pending",
                    last_ts=_as_utc(ts),
                    url=bet_url(account.platform, key, None, _meta_raw(meta)),
                )
            )

    all_entries.sort(key=lambda e: e.last_ts, reverse=True)
    stats.entries = all_entries

    in_game = [e for e in all_entries if e.status not in ("pre-game", "pending")]
    stats.bets_placed = len(in_game)
    stats.total_wagered = sum(e.wagered for e in in_game)
    decided = [e for e in in_game if e.status in ("won", "lost")]
    stats.wins = sum(1 for e in decided if e.status == "won")
    stats.losses = len(decided) - stats.wins
    winners = [e for e in decided if e.pnl is not None and e.pnl > 0]
    losers = [e for e in decided if e.pnl is not None and e.pnl < 0]
    stats.biggest_win = max(winners, key=lambda e: e.pnl, default=None)
    stats.biggest_loss = min(losers, key=lambda e: e.pnl, default=None)

    by_cat: dict[str, CategoryStat] = {}
    for e in in_game:
        cat = by_cat.setdefault(e.category or "Other", CategoryStat(name=e.category or "Other"))
        cat.wagered += e.wagered
        cat.bets += 1
        if e.status == "won":
            cat.wins += 1
        elif e.status == "lost":
            cat.losses += 1
    stats.categories = sorted(by_cat.values(), key=lambda c: c.wagered, reverse=True)
    return stats


def fill_metas(db: Session, fills: list[Fill]) -> dict[tuple[str, str], MarketMeta]:
    """MarketMeta lookup keyed by (platform, market_key) for a set of fills."""
    pairs = {(f.platform, f.market_key) for f in fills if f.market_key}
    if not pairs:
        return {}
    keys = {k for _, k in pairs}
    metas = db.scalars(select(MarketMeta).where(MarketMeta.market_key.in_(keys))).all()
    return {(m.platform, m.market_key): m for m in metas}
