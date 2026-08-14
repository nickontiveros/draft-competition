"""Fixture-backed mock connector for local demo/dev (MOCK_CONNECTORS=1).

Produces a deterministic-but-moving portfolio (seeded per account) so the
leaderboard, player pages, and stats render end-to-end without network access.
"""

from __future__ import annotations

import hashlib
import math
import time
from datetime import datetime, timedelta, timezone

from app.connectors.base import AccountState, MarketInfo, NormalizedFill, PendingOrder

_MARKETS = [
    ("mkt-fed", "Fed cuts rates in September?", "Yes", "Economics"),
    ("mkt-sb", "Chiefs vs. Ravens: who wins?", "Chiefs", "Football"),
    ("mkt-btc", "Bitcoin above $150k on Dec 31?", "Yes", "Crypto"),
    ("mkt-us-open", "Alcaraz wins the US Open?", "Yes", "Tennis"),
    ("mkt-yankees", "Yankees beat the Red Sox tonight?", "Yankees", "Baseball"),
    ("mkt-pga", "Scheffler wins the BMW Championship?", "No", "Golf"),
]


class MockConnector:
    def __init__(self, seed_key: str):
        self.seed = int(hashlib.sha256(seed_key.encode()).hexdigest()[:8], 16)

    def _market(self, n: int):
        return _MARKETS[(self.seed + n) % len(_MARKETS)]

    async def fetch_state(self) -> AccountState:
        t = time.time() / 600  # drift every ~10 minutes
        drift = 20 * math.sin(t + self.seed % 7) + 10 * math.sin(2.7 * t + self.seed % 13)
        total = 100 + drift
        cash = max(5.0, total * (0.3 + 0.1 * math.sin(self.seed + t)))
        open_keys = {self._market(i)[0] for i in range(3)}
        return AccountState(
            cash=round(cash, 2),
            positions_value=round(total - cash, 2),
            open_market_keys=open_keys,
        )

    def _trade(self, bucket: int, market_n: int, side: str, minutes_ago_base: int) -> NormalizedFill:
        key, title, outcome, _cat = self._market(market_n)
        price = ((self.seed + bucket * 37) % 80 + 10) / 100
        # Timestamps derive from the bucket, so repeated syncs dedupe cleanly.
        ts = datetime.fromtimestamp(bucket * 600, tz=timezone.utc) - timedelta(
            minutes=minutes_ago_base
        )
        return NormalizedFill(
            external_id=f"mock-{self.seed}-{bucket}-{side}",
            ts=ts,
            market_title=title,
            outcome=outcome,
            side=side,
            size=float((self.seed + bucket) % 40 + 5),
            price=price,
            market_key=key,
            raw={"mock": True},
        )

    async def fetch_fills(self, since: datetime | None = None) -> list[NormalizedFill]:
        bucket = int(time.time() // 600)  # one new trade every ~10 minutes
        fills = []
        # Rolling trades on the three open markets (indices 0-2).
        for i in range(6):
            b = bucket - i
            side = "sell" if (self.seed + b) % 4 == 0 else "buy"
            fills.append(self._trade(b, b % 3, side, minutes_ago_base=0))
        # Buys behind the two settled bets below (indices 3-4, never open).
        for j in range(2):
            b = bucket - 10 - j
            fills.append(self._trade(b, 3 + j, "buy", minutes_ago_base=0))
        return fills

    async def fetch_settlements(self, since: datetime | None = None) -> list[NormalizedFill]:
        bucket = int(time.time() // 600)
        out = []
        # Two settled bets per account: one win, one loss (payout 0).
        for j, won in enumerate((True, False)):
            b = bucket - 10 - j
            key, title, outcome, _cat = self._market(3 + j)
            size = float((self.seed + b) % 40 + 5)
            out.append(
                NormalizedFill(
                    external_id=f"mock-settle-{self.seed}-{b}",
                    ts=datetime.fromtimestamp((b + 6) * 600, tz=timezone.utc),
                    market_title=title,
                    outcome=outcome,
                    side="settle",
                    size=size,
                    price=1.0 if won else 0.0,
                    market_key=key,
                    kind="settlement",
                    notional=size * 1.0 if won else 0.0,
                    raw={"mock": True},
                )
            )
        return out

    async def fetch_open_orders(self) -> list[PendingOrder]:
        # One resting order on a market with no position (index 5: not among
        # the open markets 0-2 or the settled markets 3-4), for demo/E2E.
        key, title, outcome, _cat = self._market(5)
        size = float(self.seed % 20 + 10)
        price = (self.seed % 40 + 20) / 100
        return [
            PendingOrder(
                order_id=f"mock-order-{self.seed}",
                market_key=key,
                outcome="Yes",
                side="buy",
                size=size,
                price=price,
                reserved=round(size * price, 2),
                ts=datetime.now(timezone.utc) - timedelta(minutes=self.seed % 45),
            )
        ]

    async def fetch_market_meta(
        self, keys: list[str], hints: dict[str, dict] | None = None
    ) -> dict[str, MarketInfo]:
        by_key = {key: (title, outcome, cat) for key, title, outcome, cat in _MARKETS}
        return {
            k: MarketInfo(
                title=by_key.get(k, (k, "", ""))[0],
                category=by_key.get(k, ("", "", "Other"))[2],
                yes_sub_title=by_key.get(k, ("", "", ""))[1],
            )
            for k in keys
        }
