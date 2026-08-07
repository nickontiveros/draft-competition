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
