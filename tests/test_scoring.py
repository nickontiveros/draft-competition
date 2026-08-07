from datetime import datetime, timedelta, timezone

import pytest

import app.db as db_module
import app.sync as sync
from app.connectors.base import AccountState, NormalizedFill
from app.db import db_session, init_db
from app.models import Account, Participant, Snapshot
from app.scoring import compute_standings, lock_baselines, recent_fills


@pytest.fixture(autouse=True)
def memory_db():
    init_db(":memory:")
    yield
    db_module._engine = None
    db_module._SessionLocal = None


class StubConnector:
    def __init__(self, state: AccountState, fills=None, settlements=None):
        self.state = state
        self.fills = fills or []
        self.settlements = settlements or []

    async def fetch_state(self):
        return self.state

    async def fetch_fills(self, since=None):
        return self.fills

    async def fetch_settlements(self, since=None):
        return self.settlements

    async def fetch_market_meta(self, keys, hints=None):
        return {}


def seed(name: str, platform: str = "polymarket") -> int:
    with db_session() as db:
        p = Participant(name=name)
        db.add(p)
        db.flush()
        a = Account(participant_id=p.id, platform=platform, identifier=f"0x{name}")
        db.add(a)
        db.flush()
        return a.id


def fill(ext_id: str, side: str = "buy", size: float = 10, price: float = 0.5):
    return NormalizedFill(
        external_id=ext_id,
        ts=datetime.now(timezone.utc),
        market_title="Test market?",
        outcome="Yes",
        side=side,
        size=size,
        price=price,
    )


async def sync_with(account_id: int, connector, monkeypatch):
    monkeypatch.setattr(sync, "build_connector", lambda account: connector)
    await sync.sync_account(account_id)


async def test_baseline_pnl_and_ranking(monkeypatch):
    alice = seed("alice")
    bob = seed("bob")

    await sync_with(alice, StubConnector(AccountState(cash=40, positions_value=60)), monkeypatch)
    await sync_with(bob, StubConnector(AccountState(cash=100, positions_value=0)), monkeypatch)
    with db_session() as db:
        assert lock_baselines(db) == 2

    # Alice gains, Bob loses.
    await sync_with(alice, StubConnector(AccountState(cash=40, positions_value=85)), monkeypatch)
    await sync_with(bob, StubConnector(AccountState(cash=20, positions_value=70)), monkeypatch)

    with db_session() as db:
        standings = compute_standings(db)
    assert [s.name for s in standings] == ["alice", "bob"]
    assert standings[0].pnl == pytest.approx(25.0)
    assert standings[0].pnl_pct == pytest.approx(25.0)
    assert standings[0].game_value == pytest.approx(125.0)
    assert standings[1].pnl == pytest.approx(-10.0)
    assert standings[1].game_value == pytest.approx(90.0)
    assert standings[1].history[-1][1] == pytest.approx(-10.0)


async def test_auto_baseline_on_first_sync(monkeypatch):
    """The $100 game starts at an account's first snapshot — no admin action needed."""
    alice = seed("alice")
    await sync_with(alice, StubConnector(AccountState(cash=350, positions_value=150)), monkeypatch)

    with db_session() as db:
        standings = compute_standings(db)
    # A rich real account still enters the game at exactly $100.
    assert standings[0].pnl == pytest.approx(0.0)
    assert standings[0].game_value == pytest.approx(100.0)

    await sync_with(alice, StubConnector(AccountState(cash=350, positions_value=175)), monkeypatch)
    with db_session() as db:
        standings = compute_standings(db)
    assert standings[0].game_value == pytest.approx(125.0)


async def test_auto_baseline_uses_earliest_snapshot(monkeypatch):
    """Accounts synced before this feature keep the P&L accrued since onboarding."""
    alice = seed("alice")
    await sync_with(alice, StubConnector(AccountState(cash=100, positions_value=0)), monkeypatch)
    # Simulate a pre-feature DB: baseline was never stamped.
    with db_session() as db:
        account = db.get(Account, alice)
        account.baseline_snapshot_id = None
    await sync_with(alice, StubConnector(AccountState(cash=100, positions_value=12)), monkeypatch)

    with db_session() as db:
        standings = compute_standings(db)
    assert standings[0].pnl == pytest.approx(12.0)
    assert standings[0].game_value == pytest.approx(112.0)


async def test_fill_dedup(monkeypatch):
    alice = seed("alice")
    connector = StubConnector(
        AccountState(cash=50, positions_value=50), fills=[fill("f1"), fill("f2")]
    )
    await sync_with(alice, connector, monkeypatch)
    await sync_with(alice, connector, monkeypatch)  # same fills again

    with db_session() as db:
        assert len(recent_fills(db)) == 2


async def test_sync_error_is_isolated(monkeypatch):
    alice = seed("alice")

    class Broken:
        async def fetch_state(self):
            raise RuntimeError("api down")

        async def fetch_fills(self, since=None):
            return []

    await sync_with(alice, Broken(), monkeypatch)
    with db_session() as db:
        account = db.get(Account, alice)
        assert "api down" in account.last_sync_error
        standings = compute_standings(db)
    assert standings[0].current_total == 0  # no snapshot yet, page still renders


async def test_deposit_flag(monkeypatch):
    alice = seed("alice")
    await sync_with(alice, StubConnector(AccountState(cash=100, positions_value=0)), monkeypatch)
    # Cash jumps +$80 with no positions to settle and no sells -> flag.
    await sync_with(alice, StubConnector(AccountState(cash=180, positions_value=0)), monkeypatch)

    with db_session() as db:
        account = db.get(Account, alice)
    assert "not explained" in account.deposit_flag


async def test_no_deposit_flag_when_explained_by_sells(monkeypatch):
    alice = seed("alice")
    await sync_with(alice, StubConnector(AccountState(cash=10, positions_value=90)), monkeypatch)
    # Sold 100 contracts at 85c -> cash up $85, explained.
    sell = fill("s1", side="sell", size=100, price=0.85)
    await sync_with(
        alice, StubConnector(AccountState(cash=95, positions_value=5), fills=[sell]), monkeypatch
    )

    with db_session() as db:
        account = db.get(Account, alice)
    assert account.deposit_flag == ""
