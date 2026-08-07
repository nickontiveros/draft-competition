import json
from pathlib import Path

import httpx
import pytest

from app.connectors.polymarket import PolymarketConnector, _pick_category

FIXTURES = Path(__file__).parent / "fixtures"
WALLET = "0xAbc0000000000000000000000000000000000001"

GAMMA_EVENT = {
    "title": "Fed decision in September",
    "tags": [
        {"label": "All"},
        {"label": "Economy"},
        {"label": "Fed Rates"},
    ],
}


def make_transport():
    activity = json.loads((FIXTURES / "polymarket_activity.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "data-api.polymarket.com":
            if request.url.path == "/value":
                assert request.url.params["user"] == WALLET.lower()
                return httpx.Response(200, json=[{"user": WALLET.lower(), "value": 61.75}])
            if request.url.path == "/positions":
                return httpx.Response(
                    200, json=[{"conditionId": "0xcond1"}, {"conditionId": "0xcond9"}]
                )
            if request.url.path == "/activity":
                type_ = request.url.params.get("type")
                if type_:
                    return httpx.Response(
                        200, json=[a for a in activity if a["type"] == type_]
                    )
                return httpx.Response(200, json=activity)
        if request.url.host == "gamma-api.polymarket.com":
            assert request.url.path == "/events"
            if request.url.params.get("slug") == "fed-cuts-rates-september":
                return httpx.Response(200, json=[GAMMA_EVENT])
            return httpx.Response(200, json=[])
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
    assert state.open_market_keys == {"0xcond1", "0xcond9"}


async def test_fetch_fills_normalization(connector):
    fills = await connector.fetch_fills()
    assert len(fills) == 2  # REDEEM filtered out
    buy = next(f for f in fills if f.side == "buy")
    assert buy.external_id == "0xtx1:1234567890"
    assert buy.market_title == "Fed cuts rates in September?"
    assert buy.market_key == "0xcond1"
    assert buy.outcome == "Yes"
    assert buy.size == 40
    assert buy.price == pytest.approx(0.62)
    assert buy.notional == pytest.approx(24.8)  # usdcSize, not size*price
    assert buy.ts.tzinfo is not None


async def test_fetch_settlements(connector):
    settlements = await connector.fetch_settlements()
    assert len(settlements) == 1
    s = settlements[0]
    assert s.kind == "settlement"
    assert s.side == "settle"
    assert s.market_key == "0xcond2"
    assert s.notional == pytest.approx(15.0)


async def test_market_meta_category_from_gamma(connector):
    metas = await connector.fetch_market_meta(
        ["0xcond1", "0xcond-unknown"],
        hints={
            "0xcond1": {"eventSlug": "fed-cuts-rates-september", "title": "Fed cuts rates?"},
            "0xcond-unknown": {"title": "Mystery market"},
        },
    )
    # Specific tag wins over generic ones like "All".
    assert metas["0xcond1"].category == "Economy"
    assert metas["0xcond1"].title == "Fed cuts rates?"
    assert metas["0xcond-unknown"].category == "Other"
    assert metas["0xcond-unknown"].title == "Mystery market"


def test_pick_category_prefers_specific_sport():
    event = {"tags": [{"label": "Sports"}, {"label": "Tennis"}, {"label": "All"}]}
    assert _pick_category(event) == "Tennis"
    assert _pick_category({"tags": [{"label": "Sports"}]}) == "Sports"
    assert _pick_category({"tags": []}) == "Other"
