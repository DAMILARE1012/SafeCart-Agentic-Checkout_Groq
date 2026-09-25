"""checkout_db schema. Owned exclusively by checkout-svc."""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from commerce_common.db import make_metadata
from commerce_common.messaging import inbox_table, outbox_table

ORDER_STATUSES = (
    "CREATED",
    "AWAITING_PAYMENT",
    "PAID",
    "FULFILLED",
    "PAYMENT_FAILED",
    "EXPIRED",
    "CANCELED",
    "FULFILLMENT_FAILED",
    "REFUND_PENDING",
    "REFUNDED",
    "MANUAL_REVIEW",
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


class Base(DeclarativeBase):
    metadata = make_metadata()


class Confirmation(Base):
    """Single-use grant that lets the customer (never the agent) start payment for ONE exact quote."""

    __tablename__ = "confirmations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # the token itself is never stored
    quote_id: Mapped[str] = mapped_column(String(40), index=True)
    quote_hash: Mapped[str] = mapped_column(String(64))
    conversation_id: Mapped[str] = mapped_column(String(64))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    order_id: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    confirmation_id: Mapped[str] = mapped_column(String(40), unique=True)
    quote_id: Mapped[str] = mapped_column(String(40), unique=True)  # one order per quote, ever
    quote_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    currency: Mapped[str] = mapped_column(String(3))
    total_minor: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), default="CREATED")
    version: Mapped[int] = mapped_column(Integer, default=1)
    # commerce-svc settlement owed for this order: 'commit' (paid), 'release' (not paid) or 'void'
    # (refunded); NULL when settled.
    settlement: Mapped[str | None] = mapped_column(String(10))
    stripe_checkout_session_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(255))
    checkout_url: Mapped[str | None] = mapped_column(Text)
    failure_reason: Mapped[str | None] = mapped_column(String(120))
    # Fulfilment handoff: set when order.paid.v1 is staged; reference from fulfillment-svc.
    fulfillment_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fulfillment_reference: Mapped[str | None] = mapped_column(String(120))
    # Compensation: the Stripe refund issued when a paid order can't be fulfilled.
    stripe_refund_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    refund_status: Mapped[str | None] = mapped_column(String(20))  # Stripe's refund status, as last seen
    # Operations: when a human was alerted about this order, and when Stripe's records were cross-checked.
    attention_alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(f"status IN {ORDER_STATUSES}", name="status_valid"),
        CheckConstraint("settlement IN ('commit', 'release', 'void')", name="settlement_valid"),
        CheckConstraint("total_minor >= 0", name="total_non_negative"),
        Index("ix_orders_settlement_pending", "settlement", postgresql_where="settlement IS NOT NULL"),
        Index("ix_orders_status_created", "status", "created_at"),
    )


class WebhookEvent(Base):
    """Inbox: every verified Stripe event, stored once (event.id is the de-duplication key)."""

    __tablename__ = "webhook_inbox"

    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    type: Mapped[str] = mapped_column(String(120))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_webhook_inbox_pending", "received_at", postgresql_where="processed_at IS NULL"),
    )


class AuditLog(Base):
    """Append-only, hash-chained record of every money-relevant event (docs §9). UPDATE/DELETE are
    blocked by a database trigger; any edit to history breaks the hash chain."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor_type: Mapped[str] = mapped_column(String(20))  # user | system | webhook | service
    actor_id: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(80))
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(64), index=True)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)

    __table_args__ = (
        CheckConstraint("actor_type IN ('user', 'system', 'webhook', 'service')", name="actor_type_valid"),
    )


OUTBOX = outbox_table(Base.metadata)
INBOX = inbox_table(Base.metadata)
