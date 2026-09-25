"""fulfillment_db schema. Owned exclusively by fulfillment-svc."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from commerce_common.db import make_metadata
from commerce_common.messaging import inbox_table, outbox_table


class Base(DeclarativeBase):
    metadata = make_metadata()


class FulfillmentJob(Base):
    """One job per order (the order id is the key, which makes the consumer naturally idempotent)."""

    __tablename__ = "fulfillment_jobs"

    order_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    fulfillment_id: Mapped[str] = mapped_column(String(40), unique=True)
    status: Mapped[str] = mapped_column(String(12))  # succeeded | failed
    lines: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    carrier: Mapped[str | None] = mapped_column(String(40))
    tracking_number: Mapped[str | None] = mapped_column(String(80))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (CheckConstraint("status IN ('succeeded', 'failed')", name="status_valid"),)


OUTBOX = outbox_table(Base.metadata)
INBOX = inbox_table(Base.metadata)
