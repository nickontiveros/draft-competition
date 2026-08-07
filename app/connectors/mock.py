"""Fixture-backed mock connector for local demo/dev (MOCK_CONNECTORS=1).

Produces a deterministic-but-moving portfolio (random walk seeded per account)
so the leaderboard renders end-to-end without network access.
"""

from __future__ import annotations

import hashlib
import math
import time
from datetime import datetime, timedelta, timezone

from app.connectors.base import AccountState, NormalizedFill

_MARKETS = [
    ("Fed cuts rates in September?", "Yes"),
    ("Chiefs win the Super Bowl?", "No"),
    ("Bitcoin above $150k on Dec 31?", "Yes"),
    ("Democrats win Virginia governor race?", "Yes"),
    ("CPI above 3.0% next print?", "No"),
]


class MockConnector:
    def __init__(self, seed_key: str):
        self.seed = int(hashlib.sha256(seed_key.encode()).hexdigest()[:8], 16)

    async def fetch_state(self) -> AccountState:
        t = time.time() / 600  # drift every ~10 minutes
        drift = 20 * math.sin(t + self.seed % 7) + 10 * math.sin(2.7 * t + self.seed % 13)
        total = 100 + drift
        cash = max(5.0, total * (0.3 + 0.1 * math.sin(self.seed + t)))
        return AccountState(cash=round(cash, 2), positions_value=round(total - cash, 2))

    async def fetch_fills(self, since: datetime | None = None) -> list[NormalizedFill]:
        now = datetime.now(timezone.utc)
        fills = []
        # A handful of fills spread over recent hours, stable ids so upserts dedupe.
        for i in range(5):
            bucket = int(now.timestamp() // 3600) - i
            title, outcome = _MARKETS[(self.seed + bucket) % len(_MARKETS)]
            price = ((self.seed + bucket * 37) % 80 + 10) / 100
            fills.append(
                NormalizedFill(
                    external_id=f"mock-{self.seed}-{bucket}",
                    ts=now - timedelta(hours=i, minutes=(self.seed + bucket) % 55),
                    market_title=title,
                    outcome=outcome,
                    side="buy" if (self.seed + bucket) % 3 else "sell",
                    size=float((self.seed + bucket) % 40 + 5),
                    price=price,
                    raw={"mock": True},
                )
            )
        return fills
