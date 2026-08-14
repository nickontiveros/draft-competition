from __future__ import annotations

import json
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models import Base

_engine = None
_SessionLocal = None

# Columns added after the first release; applied via ALTER TABLE when missing
# (create_all only creates brand-new tables, it never alters existing ones).
_MIGRATIONS: dict[str, dict[str, str]] = {
    "fills": {
        "market_key": "VARCHAR(200) NOT NULL DEFAULT ''",
        "notional": "FLOAT NOT NULL DEFAULT 0",
        "kind": "VARCHAR(20) NOT NULL DEFAULT 'trade'",
        "category": "VARCHAR(80) NOT NULL DEFAULT ''",
    },
    "accounts": {
        "open_markets_json": "VARCHAR NOT NULL DEFAULT '[]'",
        "pending_orders_json": "VARCHAR NOT NULL DEFAULT '[]'",
        "last_sync_note": "VARCHAR NOT NULL DEFAULT ''",
        "baseline_adjustment": "FLOAT NOT NULL DEFAULT 0",
    },
    "snapshots": {
        "reserved": "FLOAT NOT NULL DEFAULT 0",
    },
}


def _migrate(engine) -> None:
    with engine.begin() as conn:
        for table, columns in _MIGRATIONS.items():
            existing = {
                row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))
            }
            if not existing:
                continue  # table doesn't exist yet; create_all will make it current
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def _repair_zeroed_kalshi_fills(engine) -> None:
    """Kalshi fills saved while the connector still read the retired cents
    fields (count/yes_price) have size=0; re-normalize them from the stored
    raw API payload. Sync never revisits an existing external_id, so without
    this the zeroed rows would stay wrong forever."""
    from app.connectors.kalshi import normalize_fill, normalize_settlement

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT id, kind, raw_json FROM fills "
                "WHERE platform = 'kalshi' AND size = 0 AND raw_json != ''"
            )
        ).all()
        for row_id, kind, raw_json in rows:
            try:
                raw = json.loads(raw_json)
            except ValueError:
                continue
            nf = normalize_settlement(raw) if kind == "settlement" else normalize_fill(raw)
            if nf.size:
                conn.execute(
                    text(
                        "UPDATE fills SET size = :size, price = :price, "
                        "notional = :notional WHERE id = :id"
                    ),
                    {"size": nf.size, "price": nf.price, "notional": nf.notional, "id": row_id},
                )


def _repair_missing_kalshi_market_keys(engine) -> None:
    """Fills stored before the market_key column existed were backfilled with
    '' and can never match market metadata (so their category/title stay
    stale); recover the key from the raw payload's ticker."""
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT id, raw_json FROM fills "
                "WHERE platform = 'kalshi' AND market_key = '' AND raw_json != ''"
            )
        ).all()
        for row_id, raw_json in rows:
            try:
                raw = json.loads(raw_json)
            except ValueError:
                continue
            ticker = raw.get("ticker", "")
            if ticker:
                conn.execute(
                    text("UPDATE fills SET market_key = :mk WHERE id = :id"),
                    {"mk": ticker, "id": row_id},
                )


def _repair_zero_position_baselines(engine) -> None:
    """Baseline snapshots recorded while the connector couldn't price open
    positions (e.g. Kalshi's cents-fields removal) claim $0 in positions, so
    every bet that straddled the baseline counts its full payout as P&L.
    Patch the cost basis of positions the fill ledger shows open at the
    baseline timestamp into those snapshots."""
    from datetime import datetime

    from app.scoring import open_position_cost

    def parse_ts(value):
        return value if isinstance(value, datetime) else datetime.fromisoformat(value)

    with engine.begin() as conn:
        baselines = conn.execute(
            text(
                "SELECT a.id, s.id, s.ts, s.cash FROM accounts a "
                "JOIN snapshots s ON s.id = a.baseline_snapshot_id "
                "WHERE s.positions_value = 0"
            )
        ).all()
        for account_id, snap_id, snap_ts, cash in baselines:
            fills = conn.execute(
                text(
                    "SELECT ts, market_key, side, kind, notional "
                    "FROM fills WHERE account_id = :a"
                ),
                {"a": account_id},
            ).all()
            parsed = [
                (parse_ts(ts), mk, side, kind, notional or 0.0)
                for ts, mk, side, kind, notional in fills
                if ts
            ]
            cost = open_position_cost(parsed, parse_ts(snap_ts))
            if cost > 0:
                conn.execute(
                    text(
                        "UPDATE snapshots SET positions_value = :p, total_value = :t "
                        "WHERE id = :i"
                    ),
                    {"p": round(cost, 4), "t": round((cash or 0.0) + cost, 4), "i": snap_id},
                )


def init_db(db_path: str | None = None):
    global _engine, _SessionLocal
    path = db_path or settings.db_path
    if path == ":memory:":
        # One shared connection, or each thread would see its own empty DB.
        _engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        _engine = create_engine(
            f"sqlite:///{path}", connect_args={"check_same_thread": False}
        )
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    _migrate(_engine)
    Base.metadata.create_all(_engine)
    _repair_zeroed_kalshi_fills(_engine)
    _repair_missing_kalshi_market_keys(_engine)
    _repair_zero_position_baselines(_engine)
    return _engine


@contextmanager
def db_session() -> Session:
    if _SessionLocal is None:
        init_db()
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
