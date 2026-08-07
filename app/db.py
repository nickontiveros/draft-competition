from __future__ import annotations

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
