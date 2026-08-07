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
from app.connectors.base import AccountState, NormalizedFill


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
        cash = balance["balance"] / 100  # cents -> dollars

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

        open_positions = {p["ticker"]: p["position"] for p in positions if p.get("position")}
        prices = await self._last_prices(list(open_positions))
        positions_value = 0.0
        for ticker, count in open_positions.items():
            last = prices.get(ticker, 0)  # cents for YES
            if count > 0:  # YES contracts
                positions_value += count * last / 100
            else:  # NO contracts
                positions_value += -count * (100 - last) / 100
        return AccountState(cash=cash, positions_value=positions_value)

    async def _last_prices(self, tickers: list[str]) -> dict[str, int]:
        prices: dict[str, int] = {}
        for i in range(0, len(tickers), 20):
            batch = tickers[i : i + 20]
            data = await self._get("/markets", {"tickers": ",".join(batch)})
            for m in data.get("markets", []):
                prices[m["ticker"]] = m.get("last_price", 0)
        return prices

    async def fetch_fills(self, since: datetime | None = None) -> list[NormalizedFill]:
        params: dict = {"limit": 100}
        if since is not None:
            params["min_ts"] = int(since.timestamp())
        data = await self._get("/portfolio/fills", params)
        fills = []
        for f in data.get("fills", []):
            side = f.get("side", "yes")  # "yes" | "no"
            price_cents = f.get("yes_price", 0) if side == "yes" else f.get("no_price", 0)
            fills.append(
                NormalizedFill(
                    external_id=f.get("trade_id") or f.get("fill_id", ""),
                    ts=_parse_time(f.get("created_time", "")),
                    market_title=f.get("ticker", ""),
                    outcome=side.capitalize(),
                    side=f.get("action", "").lower(),  # "buy" | "sell"
                    size=float(f.get("count", 0)),
                    price=price_cents / 100,
                    raw=f,
                )
            )
        return fills


def _parse_time(value: str) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
