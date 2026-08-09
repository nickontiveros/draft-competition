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
        assert "open_markets_json" in account_cols
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
