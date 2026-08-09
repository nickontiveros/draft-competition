"""Kalshi connector.

Auth: API key id + RSA-PSS signature per request (read-only scoped keys work).
Signed message: "<timestamp_ms><METHOD><path>" where path includes the
/trade-api/v2 prefix and excludes the query string.

Portfolio value = cash balance + sum of current position values, where a
position's current value is priced from the market's last price fetched via
the (public) batch markets endpoint. Kalshi's positions endpoint reports
signed contract counts: positive = YES contracts, negative = NO contracts.
"""

from __future__ import annotations

import base64
import datetime as dt
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.config import settings
from app.connectors.base import AccountState, MarketInfo, NormalizedFill

# Series-ticker prefixes -> sport, for when Kalshi's category is just "Sports".
SERIES_SPORTS = {
    "KXMLB": "Baseball",
    "KXNBA": "Basketball",
    "KXNCAAB": "Basketball",
    "KXWNBA": "Basketball",
    "KXNFL": "Football",
    "KXNCAAF": "Football",
    "KXNHL": "Hockey",
    "KXATP": "Tennis",
    "KXWTA": "Tennis",
    "KXPGA": "Golf",
    "KXLIV": "Golf",
    "KXUFC": "MMA",
    "KXEPL": "Soccer",
    "KXUCL": "Soccer",
    "KXMLS": "Soccer",
    "KXF1": "Motorsport",
    "KXNASCAR": "Motorsport",
}


def _fp(value) -> float:
    """Parse a Kalshi fixed-point decimal string ("5.00"); None/"" -> 0.0."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def normalize_fill(f: dict) -> NormalizedFill:
    """Normalize a raw /portfolio/fills entry. Handles both the current
    fixed-point schema (count_fp, yes_price_dollars) and the retired cents
    schema (count, yes_price)."""
    side = f.get("side") or f.get("outcome_side") or "yes"  # "yes" | "no"
    price_dollars = f.get(f"{side}_price_dollars")
    if price_dollars is not None:
        price = _fp(price_dollars)
    else:  # legacy cents fields
        price = (f.get("yes_price", 0) if side == "yes" else f.get("no_price", 0)) / 100
    if f.get("count_fp") is not None:
        size = _fp(f["count_fp"])
    else:
        size = float(f.get("count", 0))
    return NormalizedFill(
        external_id=f.get("trade_id") or f.get("fill_id", ""),
        ts=_parse_time(f.get("created_time", "")),
        market_title=f.get("ticker", ""),
        outcome=side.capitalize(),
        side=f.get("action", "").lower(),  # "buy" | "sell"
        size=size,
        price=price,
        market_key=f.get("ticker", ""),
        raw=f,
    )


def normalize_settlement(s: dict) -> NormalizedFill:
    """Normalize a raw /portfolio/settlements entry (both schema generations)."""
    ticker = s.get("ticker", "")
    if s.get("yes_count_fp") is not None or s.get("no_count_fp") is not None:
        size = _fp(s.get("yes_count_fp")) + _fp(s.get("no_count_fp"))
    else:
        size = float(s.get("yes_count", 0) or 0) + float(s.get("no_count", 0) or 0)
    revenue = float(s.get("revenue", 0)) / 100  # cents -> dollars paid out
    return NormalizedFill(
        external_id=f"settle-{ticker}-{s.get('settled_time', '')}",
        ts=_parse_time(s.get("settled_time", "")),
        market_title=ticker,
        outcome=str(s.get("market_result", "")).capitalize(),
        side="settle",
        size=size,
        price=(revenue / size) if size else 0.0,
        market_key=ticker,
        kind="settlement",
        notional=revenue,
        raw=s,
    )


def sign_pss(private_key_pem: str, message: str) -> str:
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    signature = key.sign(
        message.encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


class KalshiConnector:
    def __init__(self, api_key_id: str, private_key_pem: str, client: httpx.AsyncClient | None = None):
        self.api_key_id = api_key_id
        self.private_key_pem = private_key_pem
        self._client = client

    def _c(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30)
        return self._client

    def _headers(self, method: str, path: str) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        msg = f"{ts}{method}{path}"
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sign_pss(self.private_key_pem, msg),
        }

    async def _get(self, endpoint: str, params: dict | None = None) -> dict:
        url = f"{settings.kalshi_base_url}{endpoint}"
        path = urlparse(url).path  # includes /trade-api/v2, excludes query
        resp = await self._c().get(url, params=params, headers=self._headers("GET", path))
        resp.raise_for_status()
        return resp.json()

    async def fetch_state(self) -> AccountState:
        balance = await self._get("/portfolio/balance")
        if balance.get("balance_dollars") is not None:
            cash = _fp(balance["balance_dollars"])
        else:
            cash = balance["balance"] / 100  # legacy cents -> dollars

        positions: list[dict] = []
        cursor = None
        while True:
            params = {"limit": 200, "settlement_status": "unsettled"}
            if cursor:
                params["cursor"] = cursor
            page = await self._get("/portfolio/positions", params)
            positions.extend(page.get("market_positions", []))
            cursor = page.get("cursor")
            if not cursor:
                break

        open_positions: dict[str, float] = {}
        for p in positions:
            if p.get("position_fp") is not None:
                count = _fp(p["position_fp"])
            else:
                count = float(p.get("position") or 0)
            if count:
                open_positions[p["ticker"]] = count
        prices = await self._last_prices(list(open_positions))
        positions_value = 0.0
        for ticker, count in open_positions.items():
            last = prices.get(ticker, 0.0)  # dollars for YES
            if count > 0:  # YES contracts
                positions_value += count * last
            else:  # NO contracts
                positions_value += -count * (1 - last)
        return AccountState(
            cash=cash,
            positions_value=positions_value,
            open_market_keys=set(open_positions),
        )

    async def _markets(self, tickers: list[str]) -> dict[str, dict]:
        markets: dict[str, dict] = {}
        for i in range(0, len(tickers), 20):
            batch = tickers[i : i + 20]
            data = await self._get("/markets", {"tickers": ",".join(batch)})
            for m in data.get("markets", []):
                markets[m["ticker"]] = m
        return markets

    async def _last_prices(self, tickers: list[str]) -> dict[str, float]:
        """Last YES price per ticker, in dollars."""
        out: dict[str, float] = {}
        for t, m in (await self._markets(tickers)).items():
            if m.get("last_price_dollars") is not None:
                out[t] = _fp(m["last_price_dollars"])
            else:
                out[t] = m.get("last_price", 0) / 100  # legacy cents
        return out

    async def fetch_fills(self, since: datetime | None = None) -> list[NormalizedFill]:
        params: dict = {"limit": 100}
        if since is not None:
            params["min_ts"] = int(since.timestamp())
        fills: list[NormalizedFill] = []
        cursor = None
        while True:
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/portfolio/fills", params)
            fills.extend(normalize_fill(f) for f in data.get("fills", []))
            cursor = data.get("cursor")
            if not cursor:
                break
        return fills

    async def fetch_settlements(self, since: datetime | None = None) -> list[NormalizedFill]:
        params: dict = {"limit": 100}
        if since is not None:
            params["min_ts"] = int(since.timestamp())
        data = await self._get("/portfolio/settlements", params)
        return [normalize_settlement(s) for s in data.get("settlements", [])]

    async def fetch_market_meta(
        self, keys: list[str], hints: dict[str, dict] | None = None
    ) -> dict[str, MarketInfo]:
        markets = await self._markets(keys)
        out: dict[str, MarketInfo] = {}
        for key in keys:
            m = markets.get(key, {})
            category = m.get("category", "") or "Other"
            series_prefix = key.split("-")[0].upper()
            if series_prefix in SERIES_SPORTS and category in ("Sports", "Other"):
                category = SERIES_SPORTS[series_prefix]
            out[key] = MarketInfo(
                title=m.get("title", key),
                category=category,
                yes_sub_title=m.get("yes_sub_title", ""),
                raw=m,
            )
        return out


def _parse_time(value: str) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
