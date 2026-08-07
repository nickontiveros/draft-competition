from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass
class AccountState:
    cash: float
    positions_value: float

    @property
    def total(self) -> float:
        return self.cash + self.positions_value


@dataclass
class NormalizedFill:
    external_id: str
    ts: datetime
    market_title: str
    outcome: str  # "Yes" / "No" / outcome name
    side: str  # "buy" | "sell"
    size: float  # contracts/shares
    price: float  # dollars per share, 0..1
    raw: dict = field(default_factory=dict)


class Connector(Protocol):
    """A platform integration for one account."""

    async def fetch_state(self) -> AccountState: ...

    async def fetch_fills(self, since: datetime | None = None) -> list[NormalizedFill]: ...
