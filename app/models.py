from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Participant(Base):
    __tablename__ = "participants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    accounts: Mapped[list[Account]] = relationship(back_populates="participant")


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("platform", "identifier"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"))
    platform: Mapped[str] = mapped_column(String(20))  # "kalshi" | "polymarket"
    # polymarket: wallet address; kalshi: API key id
    identifier: Mapped[str] = mapped_column(String(200))
    # kalshi: Fernet-encrypted private key PEM (or plaintext if CRED_SECRET unset); polymarket: empty
    credentials: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Set when "lock baselines" runs; scoring measures P&L from this snapshot.
    baseline_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("snapshots.id"), nullable=True
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_sync_error: Mapped[str] = mapped_column(String, default="")
    deposit_flag: Mapped[str] = mapped_column(String, default="")
    # JSON list of market keys with an open position, refreshed each sync.
    open_markets_json: Mapped[str] = mapped_column(String, default="[]")

    @property
    def open_markets(self) -> set[str]:
        return set(json.loads(self.open_markets_json or "[]"))

    participant: Mapped[Participant] = relationship(back_populates="accounts")
    baseline_snapshot: Mapped[Snapshot | None] = relationship(foreign_keys=[baseline_snapshot_id])


class Snapshot(Base):
    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    cash: Mapped[float] = mapped_column(default=0.0)
    positions_value: Mapped[float] = mapped_column(default=0.0)
    total_value: Mapped[float] = mapped_column(default=0.0)


class Fill(Base):
    __tablename__ = "fills"
    __table_args__ = (UniqueConstraint("platform", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    platform: Mapped[str] = mapped_column(String(20))
    external_id: Mapped[str] = mapped_column(String(200))
    ts: Mapped[datetime] = mapped_column(index=True)
    market_title: Mapped[str] = mapped_column(String, default="")
    outcome: Mapped[str] = mapped_column(String(120), default="")  # "Yes"/"No"/team name
    side: Mapped[str] = mapped_column(String(10), default="")  # "buy" | "sell" | "settle"
    size: Mapped[float] = mapped_column(default=0.0)  # contracts/shares
    price: Mapped[float] = mapped_column(default=0.0)  # dollars per share (0-1)
    # Kalshi ticker / Polymarket conditionId — groups fills into one "bet".
    market_key: Mapped[str] = mapped_column(String(200), default="", index=True)
    notional: Mapped[float] = mapped_column(default=0.0)  # dollars in/out for this fill
    kind: Mapped[str] = mapped_column(String(20), default="trade")  # "trade" | "settlement"
    category: Mapped[str] = mapped_column(String(80), default="")
    raw_json: Mapped[str] = mapped_column(String, default="{}")

    account: Mapped[Account] = relationship()

    @property
    def raw(self) -> dict:
        try:
            return json.loads(self.raw_json) if self.raw_json else {}
        except ValueError:
            return {}


class MarketMeta(Base):
    """Cache of per-market metadata (title, category, what YES means)."""

    __tablename__ = "market_meta"
    __table_args__ = (UniqueConstraint("platform", "market_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(20))
    market_key: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String, default="")
    category: Mapped[str] = mapped_column(String(80), default="")
    yes_sub_title: Mapped[str] = mapped_column(String, default="")
    raw_json: Mapped[str] = mapped_column(String, default="{}")
