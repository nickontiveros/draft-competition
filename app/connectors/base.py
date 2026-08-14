from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass
class AccountState:
    cash: float
    positions_value: float
    # Market keys (Kalshi ticker / Polymarket conditionId) with an open position.
    open_market_keys: set[str] = field(default_factory=set)
    # Cash committed to resting (unfilled) buy orders. Kalshi excludes this
    # from the balance endpoint; without it, a resting order looks like a loss.
    reserved: float = 0.0

    @property
    def total(self) -> float:
        return self.cash + self.positions_value + self.reserved


@dataclass
class NormalizedFill:
    external_id: str
    ts: datetime
    market_title: str
    outcome: str  # "Yes" / "No" / outcome or team name
    side: str  # "buy" | "sell" | "settle"
    size: float  # contracts/shares
    price: float  # dollars per share, 0..1
    market_key: str = ""
    kind: str = "trade"  # "trade" | "settlement"
    notional: float = 0.0  # dollars moved; defaults to size * price
    raw: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.notional:
            self.notional = round(self.size * self.price, 4)


@dataclass
class PendingOrder:
    """A resting (unfilled) limit order — a bet placed but not yet matched."""

    order_id: str
    market_key: str
    outcome: str  # "Yes" / "No"
    side: str  # "buy" | "sell"
    size: float  # contracts still unfilled
    price: float  # limit price, dollars per contract
    reserved: float  # cash locked for this order (0 for sells)
    ts: datetime

    def as_json(self) -> dict:
        return {
            "order_id": self.order_id,
            "market_key": self.market_key,
            "outcome": self.outcome,
            "side": self.side,
            "size": self.size,
            "price": self.price,
            "reserved": self.reserved,
            "ts": self.ts.isoformat(),
        }


@dataclass
class MarketInfo:
    title: str = ""
    category: str = ""
    yes_sub_title: str = ""
    raw: dict = field(default_factory=dict)


class Connector(Protocol):
    """A platform integration for one account."""

    async def fetch_state(self) -> AccountState: ...

    async def fetch_fills(self, since: datetime | None = None) -> list[NormalizedFill]: ...

    async def fetch_settlements(self, since: datetime | None = None) -> list[NormalizedFill]: ...

    async def fetch_open_orders(self) -> list[PendingOrder]: ...

    async def fetch_market_meta(
        self, keys: list[str], hints: dict[str, dict] | None = None
    ) -> dict[str, MarketInfo]:
        """Metadata per market key. `hints` maps key -> a raw fill dict from the
        same platform (used where lookups need e.g. an event slug)."""
        ...
