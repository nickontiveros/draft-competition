import json
from pathlib import Path

import httpx
import pytest

from app.connectors.polymarket import PolymarketConnector

FIXTURES = Path(__file__).parent / "fixtures"
WALLET = "0xAbc0000000000000000000000000000000000001"


def make_transport():
    activity = json.loads((FIXTURES / "polymarket_activity.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "data-api.polymarket.com":
            if request.url.path == "/value":
                assert request.url.params["user"] == WALLET.lower()
                return httpx.Response(200, json=[{"user": WALLET.lower(), "value": 61.75}])
            if request.url.path == "/activity":
                if request.url.params.get("type") == "TRADE":
                    return httpx.Response(
                        200, json=[a for a in activity if a["type"] == "TRADE"]
                    )
                return httpx.Response(200, json=activity)
        if request.method == "POST":  # Polygon RPC
            body = json.loads(request.content)
            assert body["method"] == "eth_call"
            assert body["params"][0]["data"].endswith(WALLET.lower().removeprefix("0x"))
            # 38.25 USDC (6 decimals) from the first token, 0 from the second
            token = body["params"][0]["to"]
            value = 38_250_000 if token.startswith("0x2791") else 0
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(value)})
        raise AssertionError(f"unexpected request {request.url}")

    return httpx.MockTransport(handler)


@pytest.fixture
def connector():
    client = httpx.AsyncClient(transport=make_transport())
    return PolymarketConnector(WALLET, client=client)


async def test_fetch_state(connector):
    state = await connector.fetch_state()
    assert state.positions_value == pytest.approx(61.75)
    assert state.cash == pytest.approx(38.25)
    assert state.total == pytest.approx(100.0)


async def test_fetch_fills_normalization(connector):
    fills = await connector.fetch_fills()
    assert len(fills) == 2  # REDEEM filtered out
    buy = next(f for f in fills if f.side == "buy")
    assert buy.external_id == "0xtx1:1234567890"
    assert buy.market_title == "Fed cuts rates in September?"
    assert buy.outcome == "Yes"
    assert buy.size == 40
    assert buy.price == pytest.approx(0.62)
    assert buy.ts.year == 2025 or buy.ts.year == 2026  # parsed from unix ts, tz-aware
    assert buy.ts.tzinfo is not None


async def test_flow_events_excludes_trades(connector):
    events = await connector.fetch_flow_events()
    assert [e["type"] for e in events] == ["REDEEM"]
