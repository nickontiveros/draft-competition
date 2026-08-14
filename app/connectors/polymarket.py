"""Polymarket connector.

Uses the public Data API (no auth) keyed by wallet address:
  GET /value?user=<addr>      -> total USD value of open positions
  GET /positions?user=<addr>  -> open positions (conditionIds)
  GET /activity?user=<addr>   -> trades / splits / merges / redeems

Cash is the USDC balance of the (proxy) wallet, read via a public Polygon
JSON-RPC eth_call — no API key required. Market categories come from the
public Gamma API's event tags (e.g. Tennis, NBA).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.connectors.base import AccountState, MarketInfo, NormalizedFill

# Bridged USDC (USDC.e) on Polygon — Polymarket's collateral token.
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# Native USDC on Polygon (some wallets hold this instead).
USDC_NATIVE_ADDRESS = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
_BALANCE_OF_SELECTOR = "0x70a08231"

# Gamma tag labels too generic to use as a bet category.
_GENERIC_TAGS = {"all", "sports", "games", "new", "trending", "hide from new", "recurring"}


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
        cash, rpc_used = await self._usdc_balance()
        open_keys = await self._open_market_keys()
        return AccountState(
            cash=cash,
            positions_value=positions_value,
            open_market_keys=open_keys,
            valuation={
                "positions_api_value": round(positions_value, 4),
                "cash_rpc": round(cash, 4),
                "rpc_endpoint": rpc_used,
            },
        )

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

    async def _open_market_keys(self) -> set[str]:
        resp = await self._c().get(
            f"{settings.polymarket_data_url}/positions",
            params={"user": self.wallet, "limit": 200, "sizeThreshold": 0.5},
        )
        resp.raise_for_status()
        return {p["conditionId"] for p in resp.json() if p.get("conditionId")}

    def _rpc_urls(self) -> list[str]:
        """Configured RPC first, then public fallbacks (deduped, order kept).
        Public Polygon RPCs rate-limit; one flaky endpoint must not zero out
        a player's cash."""
        urls = [
            settings.polygon_rpc_url,
            "https://polygon-rpc.com",
            "https://polygon.llamarpc.com",
            "https://rpc.ankr.com/polygon",
        ]
        return list(dict.fromkeys(u for u in urls if u))

    async def _usdc_balance(self) -> tuple[float, str]:
        """USDC balance in dollars plus the RPC endpoint that served it.
        Raises if every endpoint fails — a sync error beats a snapshot that
        silently books cash as $0."""
        padded = self.wallet.removeprefix("0x").rjust(64, "0")
        last_error: Exception | None = None
        for url in self._rpc_urls():
            try:
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
                    resp = await self._c().post(url, json=payload)
                    resp.raise_for_status()
                    body = resp.json()
                    if "result" not in body:  # rate-limit/error payload
                        raise RuntimeError(
                            f"RPC error from {url}: {body.get('error', body)}"
                        )
                    total += int(body["result"], 16) / 1e6  # USDC has 6 decimals
                return total, url
            except Exception as exc:  # noqa: BLE001 - try the next endpoint
                last_error = exc
        raise RuntimeError(f"all Polygon RPC endpoints failed: {last_error}")

    async def _activity(self, since: datetime | None, type_: str | None) -> list[dict]:
        params: dict = {"user": self.wallet, "limit": 100, "sortBy": "TIMESTAMP"}
        if type_:
            params["type"] = type_
        if since is not None:
            params["start"] = int(since.timestamp())
        resp = await self._c().get(f"{settings.polymarket_data_url}/activity", params=params)
        resp.raise_for_status()
        return resp.json()

    async def fetch_fills(self, since: datetime | None = None) -> list[NormalizedFill]:
        fills = []
        for a in await self._activity(since, "TRADE"):
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
                    market_key=a.get("conditionId", ""),
                    notional=float(a.get("usdcSize", 0)) or 0.0,
                    raw=a,
                )
            )
        return fills

    async def fetch_settlements(self, since: datetime | None = None) -> list[NormalizedFill]:
        out = []
        for a in await self._activity(since, "REDEEM"):
            if a.get("type") != "REDEEM":
                continue
            size = float(a.get("size", 0))
            payout = float(a.get("usdcSize", 0))
            out.append(
                NormalizedFill(
                    external_id=f"{a.get('transactionHash', '')}:{a.get('asset', '')}",
                    ts=datetime.fromtimestamp(int(a["timestamp"]), tz=timezone.utc),
                    market_title=a.get("title", ""),
                    outcome=a.get("outcome", ""),
                    side="settle",
                    size=size,
                    price=(payout / size) if size else 0.0,
                    market_key=a.get("conditionId", ""),
                    kind="settlement",
                    notional=payout,
                    raw=a,
                )
            )
        return out

    async def fetch_open_orders(self) -> list:
        """Polymarket limit orders don't move USDC until matched (the wallet
        balance already reflects them), and the CLOB open-orders API needs
        trading credentials we don't hold — nothing to report."""
        return []

    async def fetch_market_meta(
        self, keys: list[str], hints: dict[str, dict] | None = None
    ) -> dict[str, MarketInfo]:
        hints = hints or {}
        out: dict[str, MarketInfo] = {}
        for key in keys:
            hint = hints.get(key, {})
            title = hint.get("title", "")
            category = "Other"
            slug = hint.get("eventSlug") or hint.get("slug", "")
            if slug:
                try:
                    resp = await self._c().get(
                        f"{settings.polymarket_gamma_url}/events", params={"slug": slug}
                    )
                    resp.raise_for_status()
                    events = resp.json()
                    if events:
                        event = events[0]
                        title = title or event.get("title", "")
                        category = _pick_category(event)
                        out[key] = MarketInfo(title=title, category=category, raw=event)
                        continue
                except httpx.HTTPError:
                    pass  # metadata is best-effort; fall through to the hint
            out[key] = MarketInfo(title=title or key, category=category, raw=hint)
        return out


def _pick_category(event: dict) -> str:
    labels = [t.get("label", "") for t in event.get("tags", []) if t.get("label")]
    specific = [l for l in labels if l.lower() not in _GENERIC_TAGS]
    if specific:
        return specific[0]
    if any(l.lower() == "sports" for l in labels):
        return "Sports"
    return labels[0] if labels else "Other"
