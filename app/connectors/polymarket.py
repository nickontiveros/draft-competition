"""Polymarket connector.

Uses the public Data API (no auth) keyed by wallet address:
  GET /value?user=<addr>      -> total USD value of open positions
  GET /activity?user=<addr>   -> trades / splits / merges / redeems

Cash is the USDC.e balance of the (proxy) wallet, read via a public Polygon
JSON-RPC eth_call — no API key required.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.connectors.base import AccountState, NormalizedFill

# Bridged USDC (USDC.e) on Polygon — Polymarket's collateral token.
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# Native USDC on Polygon (some wallets hold this instead).
USDC_NATIVE_ADDRESS = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
_BALANCE_OF_SELECTOR = "0x70a08231"


class PolymarketConnector:
    def __init__(self, wallet_address: str, client: httpx.AsyncClient | None = None):
        self.wallet = wallet_address.lower()
        self._client = client

    def _c(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30)
        return self._client

    async def fetch_state(self) -> AccountState:
        positions_value = await self._positions_value()
        cash = await self._usdc_balance()
        return AccountState(cash=cash, positions_value=positions_value)

    async def _positions_value(self) -> float:
        resp = await self._c().get(
            f"{settings.polymarket_data_url}/value", params={"user": self.wallet}
        )
        resp.raise_for_status()
        data = resp.json()
        # API returns [{"user": ..., "value": ...}] (or a bare object).
        if isinstance(data, list):
            return float(data[0]["value"]) if data else 0.0
        return float(data.get("value", 0.0))

    async def _usdc_balance(self) -> float:
        padded = self.wallet.removeprefix("0x").rjust(64, "0")
        total = 0.0
        for token in (USDC_E_ADDRESS, USDC_NATIVE_ADDRESS):
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_call",
                "params": [
                    {"to": token, "data": _BALANCE_OF_SELECTOR + padded},
                    "latest",
                ],
            }
            resp = await self._c().post(settings.polygon_rpc_url, json=payload)
            resp.raise_for_status()
            result = resp.json().get("result", "0x0")
            total += int(result, 16) / 1e6  # USDC has 6 decimals
        return total

    async def fetch_fills(self, since: datetime | None = None) -> list[NormalizedFill]:
        params: dict = {"user": self.wallet, "type": "TRADE", "limit": 100, "sortBy": "TIMESTAMP"}
        if since is not None:
            params["start"] = int(since.timestamp())
        resp = await self._c().get(f"{settings.polymarket_data_url}/activity", params=params)
        resp.raise_for_status()
        fills = []
        for a in resp.json():
            if a.get("type") != "TRADE":
                continue
            fills.append(
                NormalizedFill(
                    external_id=f"{a.get('transactionHash', '')}:{a.get('asset', '')}",
                    ts=datetime.fromtimestamp(int(a["timestamp"]), tz=timezone.utc),
                    market_title=a.get("title", ""),
                    outcome=a.get("outcome", ""),
                    side=a.get("side", "").lower(),
                    size=float(a.get("size", 0)),
                    price=float(a.get("price", 0)),
                    raw=a,
                )
            )
        return fills

    async def fetch_flow_events(self, since: datetime | None = None) -> list[dict]:
        """Non-trade activity (splits/merges/redeems) — used by deposit detection
        to explain cash changes that aren't trades."""
        params: dict = {"user": self.wallet, "limit": 100}
        if since is not None:
            params["start"] = int(since.timestamp())
        resp = await self._c().get(f"{settings.polymarket_data_url}/activity", params=params)
        resp.raise_for_status()
        return [a for a in resp.json() if a.get("type") != "TRADE"]
