import base64
import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.connectors.kalshi import KalshiConnector, sign_pss

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def rsa_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return key, pem


def test_sign_pss_verifies(rsa_key):
    key, pem = rsa_key
    message = "1754500000000GET/trade-api/v2/portfolio/balance"
    signature = base64.b64decode(sign_pss(pem, message))
    # Raises InvalidSignature on mismatch
    key.public_key().verify(
        signature,
        message.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def make_transport(rsa_public_key):
    def handler(request: httpx.Request) -> httpx.Response:
        # Every request must carry a valid signature over ts + method + path.
        ts = request.headers["KALSHI-ACCESS-TIMESTAMP"]
        msg = f"{ts}GET{request.url.path}"
        rsa_public_key.verify(
            base64.b64decode(request.headers["KALSHI-ACCESS-SIGNATURE"]),
            msg.encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        assert request.headers["KALSHI-ACCESS-KEY"] == "key-id-1"
        assert request.url.path.startswith("/trade-api/v2")

        route = request.url.path.removeprefix("/trade-api/v2")
        if route == "/portfolio/balance":
            return httpx.Response(200, json={"balance": 3899, "balance_dollars": "38.99"})
        if route == "/portfolio/positions":
            return httpx.Response(200, json=json.loads((FIXTURES / "kalshi_positions.json").read_text()))
        if route == "/markets":
            requested = set(request.url.params["tickers"].split(","))
            data = json.loads((FIXTURES / "kalshi_markets.json").read_text())
            data["markets"] = [m for m in data["markets"] if m["ticker"] in requested]
            return httpx.Response(200, json=data)
        if route == "/portfolio/fills":
            return httpx.Response(200, json=json.loads((FIXTURES / "kalshi_fills.json").read_text()))
        if route == "/portfolio/settlements":
            return httpx.Response(200, json=json.loads((FIXTURES / "kalshi_settlements.json").read_text()))
        if route == "/portfolio/orders":
            assert request.url.params["status"] == "resting"
            return httpx.Response(200, json=json.loads((FIXTURES / "kalshi_orders.json").read_text()))
        if route.startswith("/series/"):
            ticker = route.removeprefix("/series/")
            series = json.loads((FIXTURES / "kalshi_series.json").read_text())
            if ticker in series:
                return httpx.Response(200, json={"series": series[ticker]})
            return httpx.Response(404, json={"error": {"code": "not_found"}})
        raise AssertionError(f"unexpected route {route}")

    return httpx.MockTransport(handler)


@pytest.fixture
def connector(rsa_key):
    key, pem = rsa_key
    client = httpx.AsyncClient(transport=make_transport(key.public_key()))
    return KalshiConnector("key-id-1", pem, client=client)


async def test_fetch_state(connector):
    state = await connector.fetch_state()
    assert state.cash == pytest.approx(38.99)
    # 25 YES @ 70c = 17.50, plus 10 NO with yes last_price 40c -> 10 * 0.60 = 6.00
    assert state.positions_value == pytest.approx(23.50)
    assert state.total == pytest.approx(62.49)
    assert state.open_market_keys == {"FED-25SEP-CUT", "CPI-26AUG-A3"}


async def test_fetch_settlements(connector):
    settlements = await connector.fetch_settlements()
    assert len(settlements) == 2
    win = next(s for s in settlements if s.market_key == "KXMLB-26AUG07-NYY")
    assert win.kind == "settlement"
    assert win.notional == pytest.approx(20.0)  # 2000 cents revenue
    assert win.size == 20
    loss = next(s for s in settlements if s.market_key == "CPI-26JUL-A3")
    assert loss.notional == 0.0


async def test_market_meta_titles_categories_and_sport_mapping(connector):
    metas = await connector.fetch_market_meta(
        [
            "FED-25SEP-CUT",
            "KXMLB-26AUG07-NYY",
            "KXATPMATCH-26AUG06BERSHE-SHE",
            "KXLEAGUESCUP-26AUG08-MIA",
            "GONE-MKT",
        ]
    )
    # No series entry: falls back to the market's own (legacy) category.
    fed = metas["FED-25SEP-CUT"]
    assert fed.title == "Fed cuts rates in September?"
    assert fed.category == "Economics"
    assert fed.yes_sub_title == "Rates cut by 25bps or more"
    # Series says "Sports"; the ticker prefix refines it to the actual sport.
    assert metas["KXMLB-26AUG07-NYY"].category == "Baseball"
    assert metas["KXMLB-26AUG07-NYY"].yes_sub_title == "Yankees win"
    # Real series tickers extend the mapped prefixes (KXATPMATCH vs KXATP).
    assert metas["KXATPMATCH-26AUG06BERSHE-SHE"].category == "Tennis"
    # Prefix not in the map at all: the series tags name the sport.
    assert metas["KXLEAGUESCUP-26AUG08-MIA"].category == "Soccer"
    # Unknown market degrades to ticker-as-title.
    assert metas["GONE-MKT"].title == "GONE-MKT"
    assert metas["GONE-MKT"].category == "Other"


async def test_fetch_fills_normalization(connector):
    # Fixture mixes current fixed-point fields (t-111) with legacy cents
    # fields (t-222) — both schemas must normalize identically.
    fills = await connector.fetch_fills()
    assert len(fills) == 2
    yes_buy = next(f for f in fills if f.external_id == "t-111")
    assert yes_buy.side == "buy"
    assert yes_buy.outcome == "Yes"
    assert yes_buy.price == pytest.approx(0.62)  # from yes_price_dollars
    assert yes_buy.size == 25  # from count_fp
    assert yes_buy.notional == pytest.approx(15.50)
    no_sell = next(f for f in fills if f.external_id == "t-222")
    assert no_sell.price == pytest.approx(0.55)  # legacy no_price cents for NO-side fills
    assert no_sell.side == "sell"
    assert no_sell.size == 10


async def test_fetch_open_orders(connector):
    orders = await connector.fetch_open_orders()
    # ord-4 is canceled and filtered out.
    assert [o.order_id for o in orders] == ["ord-1", "ord-2", "ord-3"]

    fp_buy = orders[0]  # fixed-point schema
    assert fp_buy.market_key == "KXFEDDECISION-26SEP"
    assert fp_buy.outcome == "Yes"
    assert fp_buy.side == "buy"
    assert fp_buy.size == 40
    assert fp_buy.price == 0.35
    assert fp_buy.reserved == 14.0  # 40 x $0.35 locked

    legacy_buy = orders[1]  # legacy cents schema, NO side
    assert legacy_buy.price == 0.45
    assert legacy_buy.reserved == 4.5

    sell = orders[2]  # sells reserve contracts, not cash
    assert sell.reserved == 0.0
