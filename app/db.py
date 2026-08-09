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
