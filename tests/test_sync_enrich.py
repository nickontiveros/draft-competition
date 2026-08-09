import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

import app.db as db_module
from app.db import db_session, init_db
from app.models import Fill, MarketMeta
from app.connectors.base import MarketInfo
from app.sync import _enrich_market_meta

NOW = datetime.now(timezone.utc)
TENNIS_KEY = "KXATPMATCH-26AUG06BERSHE-SHE"


@pytest.fixture(autouse=True)
def memory_db():
    init_db(":memory:")
    yield
    db_module._engine = None
    db_module._SessionLocal = None


class RecordingConnector:
    def __init__(self, metas):
        self.metas = metas
        self.requested = []
        self.hints = None

    async def fetch_market_meta(self, keys, hints=None):
        self.requested.extend(keys)
        self.hints = hints
        return {k: self.metas[k] for k in keys if k in self.metas}


async def test_enrich_retries_and_heals_other_categories():
    """Markets cached with category "Other" (e.g. from the pre-series-endpoint
    connector) are re-fetched on later syncs — even when no current event
    references them — and both the meta cache and stored fills get healed."""
    with db_session() as db:
        db.add(
            Fill(
                account_id=1,
                platform="kalshi",
                external_id="t-she",
                ts=NOW,
                market_title=TENNIS_KEY,
                outcome="Yes",
                side="buy",
                size=4,
                price=0.74,
                market_key=TENNIS_KEY,
                notional=2.96,
                kind="trade",
                category="Other",
                raw_json=json.dumps({"ticker": TENNIS_KEY}),
            )
        )
        db.add(
            MarketMeta(
                platform="kalshi",
                market_key=TENNIS_KEY,
                title=TENNIS_KEY,
                category="Other",
                yes_sub_title="",
                raw_json="{}",
            )
        )

    connector = RecordingConnector(
        {
            TENNIS_KEY: MarketInfo(
                title="Will Ben Shelton win the match?",
                category="Tennis",
                yes_sub_title="Ben Shelton",
            )
        }
    )
    await _enrich_market_meta(connector, "kalshi", events=[])

    assert connector.requested == [TENNIS_KEY]
    assert connector.hints == {TENNIS_KEY: {"ticker": TENNIS_KEY}}
    with db_session() as db:
        metas = db.scalars(select(MarketMeta)).all()
        assert len(metas) == 1  # updated in place, not duplicated
        assert metas[0].category == "Tennis"
        assert metas[0].title == "Will Ben Shelton win the match?"
        f = db.scalars(select(Fill)).one()
        assert f.category == "Tennis"
        assert f.market_title == "Will Ben Shelton win the match?"


async def test_enrich_leaves_resolved_categories_alone():
    with db_session() as db:
        db.add(
            Fill(
                account_id=1,
                platform="kalshi",
                external_id="t-fed",
                ts=NOW,
                market_title="Fed cuts rates?",
                outcome="Yes",
                side="buy",
                size=10,
                price=0.62,
                market_key="FED-25SEP-CUT",
                notional=6.2,
                kind="trade",
                category="Economics",
                raw_json="{}",
            )
        )
        db.add(
            MarketMeta(
                platform="kalshi",
                market_key="FED-25SEP-CUT",
                title="Fed cuts rates?",
                category="Economics",
                yes_sub_title="",
                raw_json="{}",
            )
        )

    connector = RecordingConnector({})
    await _enrich_market_meta(connector, "kalshi", events=[])
    assert connector.requested == []  # nothing stale, no meta fetch at all
