import json
from datetime import datetime, timedelta, timezone

import pytest

import app.db as db_module
from app.db import db_session, init_db
from app.models import Account, Fill, MarketMeta, Participant, Snapshot
from app.stats import compute_player_stats

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def memory_db():
    init_db(":memory:")
    yield
    db_module._engine = None
    db_module._SessionLocal = None


def test_polymarket_entry_gets_event_url():
    with db_session() as db:
        p = Participant(name="poly")
        db.add(p)
        db.flush()
        a = Account(participant_id=p.id, platform="polymarket", identifier="0xabc")
        db.add(a)
        db.flush()
        f = fill(a.id, "0xcond1", "buy", 5.0, 10, NOW - timedelta(hours=1))
        f.platform = "polymarket"
        f.raw_json = json.dumps({"eventSlug": "mlb-nyy-bos"})
        db.add(f)
        db.flush()
        stats = compute_player_stats(db, db.get(Participant, p.id))
    (entry,) = stats.entries
    assert entry.url == "https://polymarket.com/event/mlb-nyy-bos"


def fill(account_id, market_key, side, notional, size, ts, kind="trade", category="", outcome="Yes"):
    return Fill(
        account_id=account_id,
        platform="kalshi",
        external_id=f"{market_key}-{side}-{ts.timestamp()}",
        ts=ts,
        market_title=market_key,
        outcome=outcome,
        side=side,
        size=size,
        price=(notional / size) if size else 0,
        market_key=market_key,
        notional=notional,
        kind=kind,
        category=category,
    )


@pytest.fixture
def player():
    with db_session() as db:
        p = Participant(name="nick")
        db.add(p)
        db.flush()
        a = Account(
            participant_id=p.id,
            platform="kalshi",
            identifier="key-1",
            open_markets_json=json.dumps(["mkt-open"]),
        )
        db.add(a)
        db.flush()
        baseline = Snapshot(account_id=a.id, ts=NOW - timedelta(days=2), total_value=100)
        db.add(baseline)
        db.flush()
        a.baseline_snapshot_id = baseline.id

        db.add_all(
            [
                # Open bet: $10 on 20 contracts, still held.
                fill(a.id, "mkt-open", "buy", 10.0, 20, NOW - timedelta(hours=5), category="Tennis"),
                # Won bet: $8 in, $16 back at settlement.
                fill(a.id, "mkt-won", "buy", 8.0, 16, NOW - timedelta(days=1), category="Baseball"),
                fill(a.id, "mkt-won", "settle", 16.0, 16, NOW - timedelta(hours=2), kind="settlement"),
                # Lost bet: $5 in, position closed with nothing back.
                fill(a.id, "mkt-lost", "buy", 5.0, 10, NOW - timedelta(days=1, hours=3), category="Baseball"),
                # Pre-game bet: bought before the baseline; settles during the game.
                fill(a.id, "mkt-pre", "buy", 30.0, 60, NOW - timedelta(days=10), category="Politics"),
                fill(a.id, "mkt-pre", "settle", 60.0, 60, NOW - timedelta(hours=1), kind="settlement"),
            ]
        )
        db.add(
            MarketMeta(
                platform="kalshi",
                market_key="mkt-won",
                title="Yankees beat the Red Sox?",
                category="Baseball",
                yes_sub_title="Yankees win",
            )
        )
        return p.id


def test_ledger_statuses_and_stats(player):
    with db_session() as db:
        p = db.get(Participant, player)
        stats = compute_player_stats(db, p)

    by_key = {e.market_key: e for e in stats.entries}
    assert by_key["mkt-open"].status == "open"
    assert by_key["mkt-open"].pnl is None
    assert by_key["mkt-won"].status == "won"
    assert by_key["mkt-won"].pnl == pytest.approx(8.0)
    assert by_key["mkt-lost"].status == "lost"
    assert by_key["mkt-lost"].pnl == pytest.approx(-5.0)
    assert by_key["mkt-pre"].status == "pre-game"

    # Pre-game excluded from every stat.
    assert stats.bets_placed == 3
    assert stats.total_wagered == pytest.approx(23.0)
    assert stats.wins == 1 and stats.losses == 1
    assert stats.win_rate == pytest.approx(50.0)
    assert stats.biggest_win.market_key == "mkt-won"
    assert stats.biggest_loss.market_key == "mkt-lost"


def test_category_breakdown(player):
    with db_session() as db:
        p = db.get(Participant, player)
        stats = compute_player_stats(db, p)

    cats = {c.name: c for c in stats.categories}
    assert set(cats) == {"Tennis", "Baseball"}  # Politics bet is pre-game
    assert cats["Baseball"].wagered == pytest.approx(13.0)
    assert cats["Baseball"].wins == 1 and cats["Baseball"].losses == 1
    assert cats["Tennis"].wagered == pytest.approx(10.0)


def test_meta_enriches_title_and_outcome(player):
    with db_session() as db:
        p = db.get(Participant, player)
        stats = compute_player_stats(db, p)

    won = next(e for e in stats.entries if e.market_key == "mkt-won")
    assert won.title == "Yankees beat the Red Sox?"
    assert won.outcome_label == "Yankees win"  # Yes + yes_sub_title


def test_pending_orders_render_but_do_not_count(player):
    with db_session() as db:
        a = db.query(Account).filter_by(identifier="key-1").first()
        a.pending_orders_json = json.dumps(
            [
                {
                    "order_id": "ord-9",
                    "market_key": "mkt-won",  # meta exists -> title/category resolve
                    "outcome": "Yes",
                    "side": "buy",
                    "size": 40.0,
                    "price": 0.35,
                    "reserved": 14.0,
                    "ts": NOW.isoformat(),
                }
            ]
        )
    with db_session() as db:
        p = db.get(Participant, player)
        stats = compute_player_stats(db, p)

    pending = [e for e in stats.entries if e.status == "pending"]
    assert len(pending) == 1
    assert pending[0].title == "Yankees beat the Red Sox?"  # enriched via MarketMeta
    assert pending[0].wagered == pytest.approx(14.0)
    assert pending[0].pnl is None
    # Excluded from every stat: same totals as without the order.
    assert stats.bets_placed == 3
    assert stats.total_wagered == pytest.approx(23.0)
    assert stats.win_rate == pytest.approx(50.0)
