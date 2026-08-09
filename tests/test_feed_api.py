import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app.db import db_session, init_db
from app.main import app
from app.models import Account, Fill, MarketMeta, Participant

NOW = datetime.now(timezone.utc)

# No context manager: entering it would run the lifespan, which re-inits the
# DB at settings.db_path and starts the sync scheduler.
client = TestClient(app)


@pytest.fixture(autouse=True)
def memory_db():
    init_db(":memory:")
    with db_session() as db:
        p = Participant(name="kneek")
        db.add(p)
        db.flush()
        acct = Account(participant_id=p.id, platform="kalshi", identifier="key-1")
        db.add(acct)
        db.flush()
        db.add(
            Fill(
                account_id=acct.id,
                platform="kalshi",
                external_id="t-she",
                ts=NOW,
                market_title="KXATPMATCH-26AUG06BERSHE-SHE",
                outcome="Yes",
                side="buy",
                size=4,
                price=0.74,
                market_key="KXATPMATCH-26AUG06BERSHE-SHE",
                notional=2.96,
                kind="trade",
                category="Tennis",
                raw_json=json.dumps({"ticker": "KXATPMATCH-26AUG06BERSHE-SHE"}),
            )
        )
        db.add(
            MarketMeta(
                platform="kalshi",
                market_key="KXATPMATCH-26AUG06BERSHE-SHE",
                title="Will Ben Shelton win the match?",
                category="Tennis",
                yes_sub_title="Ben Shelton",
                raw_json="{}",
            )
        )
    yield
    db_module._engine = None
    db_module._SessionLocal = None


def test_api_feed_shape():
    data = client.get("/api/feed").json()
    assert "as_of" in data
    (item,) = data["feed"]
    assert item["id"] == "kalshi:t-she"
    assert item["icon"] == "tennis"
    assert item["url"] == "https://kalshi.com/markets/kxatpmatch"
    assert item["who"] == "kneek"
    assert item["tone"] == "buy"
    assert item["title"] == "Will Ben Shelton win the match?"
    assert item["ts"].startswith(str(NOW.year))
    for key in ("headline", "detail", "ago", "market_key", "outcome", "side",
                "kind", "price", "notional", "category", "platform", "participant_id"):
        assert key in item


def test_standings_partial_is_fragment():
    resp = client.get("/partial/standings")
    assert resp.status_code == 200
    assert "kneek" in resp.text
    assert "<html" not in resp.text


def test_leaderboard_page_rebranded_and_live():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Fantasy Football Draft Competition 2026" in resp.text
    assert 'data-id="kalshi:t-she"' in resp.text
    assert "Ride it" in resp.text and "Fade it" in resp.text
    # The old hard reload is gone; polling script and audio engine are in.
    assert "location.reload" not in resp.text
    assert "/api/feed" in resp.text
    assert "StadiumAudio" in resp.text


def test_player_page_has_icons_and_links():
    resp = client.get("/player/1")
    assert resp.status_code == 200
    assert "Fantasy Football Draft Competition 2026" in resp.text
    assert "sicon-tennis" in resp.text
    assert "https://kalshi.com/markets/kxatpmatch" in resp.text
