import json
import sqlite3

import app.db as db_module
from app.db import init_db

OLD_SCHEMA = """
CREATE TABLE participants (
    id INTEGER PRIMARY KEY, name VARCHAR(80), created_at DATETIME
);
CREATE TABLE snapshots (
    id INTEGER PRIMARY KEY, account_id INTEGER, ts DATETIME,
    cash FLOAT, positions_value FLOAT, total_value FLOAT
);
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY, participant_id INTEGER, platform VARCHAR(20),
    identifier VARCHAR(200), credentials VARCHAR, created_at DATETIME,
    baseline_snapshot_id INTEGER, last_sync_at DATETIME,
    last_sync_error VARCHAR, deposit_flag VARCHAR
);
CREATE TABLE fills (
    id INTEGER PRIMARY KEY, account_id INTEGER, platform VARCHAR(20),
    external_id VARCHAR(200), ts DATETIME, market_title VARCHAR,
    outcome VARCHAR(80), side VARCHAR(10), size FLOAT, price FLOAT,
    raw_json VARCHAR
);
"""


def test_old_db_gains_new_columns(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO fills (account_id, platform, external_id, market_title) "
        "VALUES (1, 'kalshi', 't-1', 'FED-25SEP')"
    )
    conn.commit()
    conn.close()

    init_db(str(path))
    try:
        conn = sqlite3.connect(path)
        fill_cols = {r[1] for r in conn.execute("PRAGMA table_info(fills)")}
        assert {"market_key", "notional", "kind", "category"} <= fill_cols
        account_cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}
        assert {
            "open_markets_json",
            "pending_orders_json",
            "last_sync_note",
            "baseline_adjustment",
            "valuation_json",
        } <= account_cols
        adj = conn.execute("SELECT baseline_adjustment FROM accounts LIMIT 1").fetchone()
        assert adj is None or adj[0] == 0.0  # default applies to existing rows
        snapshot_cols = {r[1] for r in conn.execute("PRAGMA table_info(snapshots)")}
        assert "reserved" in snapshot_cols
        # Existing rows survive with sane defaults.
        row = conn.execute(
            "SELECT market_title, kind, notional, category FROM fills"
        ).fetchone()
        assert row == ("FED-25SEP", "trade", 0.0, "")
        # The brand-new table appears via create_all.
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "market_meta" in tables
        conn.close()
    finally:
        db_module._engine = None
        db_module._SessionLocal = None


def test_pre_migration_fills_regain_market_key_from_raw(tmp_path):
    """Fills stored before the market_key column existed (backfilled to '')
    get their key recovered from the raw payload's ticker at startup."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO fills (account_id, platform, external_id, market_title, ts, raw_json)"
        " VALUES (1, 'kalshi', 't-old', 'KXATPMATCH-26AUG06BERSHE-SHE',"
        " '2026-08-07 12:00:00', ?)",
        (json.dumps({"trade_id": "t-old", "ticker": "KXATPMATCH-26AUG06BERSHE-SHE"}),),
    )
    conn.commit()
    conn.close()

    init_db(str(path))
    try:
        conn = sqlite3.connect(path)
        mk = conn.execute("SELECT market_key FROM fills WHERE external_id='t-old'").fetchone()[0]
        assert mk == "KXATPMATCH-26AUG06BERSHE-SHE"
        conn.close()
    finally:
        db_module._engine = None
        db_module._SessionLocal = None


def test_zeroed_kalshi_fills_repaired_from_raw(tmp_path):
    """Rows saved while the connector read Kalshi's retired cents fields
    (size/price/notional all 0) get re-normalized from raw_json at startup."""
    path = tmp_path / "tracker.db"
    init_db(str(path))
    db_module._engine = None
    db_module._SessionLocal = None

    zeroed_fill_raw = json.dumps(
        {
            "trade_id": "t-miami",
            "ticker": "KXMIA-26AUG08",
            "side": "no",
            "action": "buy",
            "count_fp": "10.00",
            "yes_price_dollars": "0.50",
            "no_price_dollars": "0.50",
            "created_time": "2026-08-08T20:00:00Z",
        }
    )
    zeroed_settle_raw = json.dumps(
        {
            "ticker": "KXMIA-26AUG08",
            "market_result": "no",
            "yes_count_fp": "0.00",
            "no_count_fp": "10.00",
            "revenue": 588,
            "settled_time": "2026-08-09T01:00:00Z",
        }
    )
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO fills (account_id, platform, external_id, market_title, outcome,"
        " side, size, price, market_key, notional, kind, category, raw_json, ts)"
        " VALUES (1, 'kalshi', 't-miami', 'KXMIA-26AUG08', 'No', 'buy', 0, 0,"
        " 'KXMIA-26AUG08', 0, 'trade', '', ?, '2026-08-08 20:00:00')",
        (zeroed_fill_raw,),
    )
    conn.execute(
        "INSERT INTO fills (account_id, platform, external_id, market_title, outcome,"
        " side, size, price, market_key, notional, kind, category, raw_json, ts)"
        " VALUES (1, 'kalshi', 'settle-KXMIA-26AUG08', 'KXMIA-26AUG08', 'No', 'settle',"
        " 0, 0, 'KXMIA-26AUG08', 5.88, 'settlement', '', ?, '2026-08-09 01:00:00')",
        (zeroed_settle_raw,),
    )
    # A healthy row and a non-kalshi row must be left untouched.
    conn.execute(
        "INSERT INTO fills (account_id, platform, external_id, market_title, outcome,"
        " side, size, price, market_key, notional, kind, category, raw_json, ts)"
        " VALUES (2, 'polymarket', 'p-1', 'Some market', 'Yes', 'buy', 4, 0.25,"
        " 'cond-1', 1.0, 'trade', '', '{}', '2026-08-08 12:00:00')"
    )
    conn.commit()
    conn.close()

    init_db(str(path))
    try:
        conn = sqlite3.connect(path)
        rows = {
            r[0]: r[1:]
            for r in conn.execute("SELECT external_id, size, price, notional FROM fills")
        }
        assert rows["t-miami"] == (10.0, 0.50, 5.0)  # $5 bet: 10 NO @ 50c
        assert rows["settle-KXMIA-26AUG08"] == (10.0, 0.588, 5.88)
        assert rows["p-1"] == (4.0, 0.25, 1.0)
        conn.close()
    finally:
        db_module._engine = None
        db_module._SessionLocal = None


def test_zero_position_baseline_repaired_from_ledger(tmp_path):
    """Baselines stamped while the connector priced open positions at $0
    (production bug: P&L showed +$7.58 instead of +$2.51) get the open
    position's cost basis patched in at startup."""
    from datetime import datetime, timedelta, timezone

    from app.db import db_session
    from app.models import Account, Fill, Participant, Snapshot

    path = tmp_path / "tracker.db"
    init_db(str(path))
    baseline_ts = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    with db_session() as db:
        p = Participant(name="kneek")
        db.add(p)
        db.flush()
        a = Account(participant_id=p.id, platform="kalshi", identifier="k")
        db.add(a)
        db.flush()
        snap = Snapshot(
            account_id=a.id, ts=baseline_ts, cash=370.60,
            positions_value=0.0, total_value=370.60,  # recorded mid-breakage
        )
        db.add(snap)
        db.flush()
        a.baseline_snapshot_id = snap.id
        db.add_all([
            Fill(account_id=a.id, platform="kalshi", external_id="t-she-1",
                 ts=baseline_ts - timedelta(days=1), market_title="SHE", outcome="Yes",
                 side="buy", size=7, price=0.70, market_key="KXATPMATCH-X",
                 notional=4.90, kind="trade"),
            Fill(account_id=a.id, platform="kalshi", external_id="settle-she",
                 ts=baseline_ts + timedelta(days=1), market_title="SHE", outcome="Yes",
                 side="settle", size=7, price=0.947, market_key="KXATPMATCH-X",
                 notional=6.63, kind="settlement"),
        ])
    db_module._engine = None
    db_module._SessionLocal = None

    init_db(str(path))  # startup repair runs here
    try:
        conn = sqlite3.connect(path)
        pv, total = conn.execute(
            "SELECT positions_value, total_value FROM snapshots"
        ).fetchone()
        assert pv == 4.90
        assert total == 375.50
        conn.close()
    finally:
        db_module._engine = None
        db_module._SessionLocal = None
