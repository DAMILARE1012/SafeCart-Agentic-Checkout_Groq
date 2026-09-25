"""The order state machine (docs §7.1): the only authoritative order state."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from checkout_svc import audit
from checkout_svc.models import Order
from commerce_common.errors import Conflict, NotFound

# Allowed transitions. Anything else is rejected, which also makes late/out-of-order webhooks harmless.
TRANSITIONS: dict[str, frozenset[str]] = {
    "CREATED": frozenset({"AWAITING_PAYMENT", "CANCELED"}),
    "AWAITING_PAYMENT": frozenset({"PAID", "PAYMENT_FAILED", "EXPIRED", "MANUAL_REVIEW"}),
    "PAID": frozenset({"FULFILLED", "FULFILLMENT_FAILED", "MANUAL_REVIEW"}),  # fulfilment arrives in M4
    "FULFILLMENT_FAILED": frozenset({"REFUND_PENDING"}),
    "REFUND_PENDING": frozenset({"REFUNDED", "MANUAL_REVIEW"}),
    # Money arriving for an order we consider dead must never be ignored: a human (or M5 auto-refund) acts.
    "CANCELED": frozenset({"MANUAL_REVIEW"}),
    "EXPIRED": frozenset({"MANUAL_REVIEW"}),
    "PAYMENT_FAILED": frozenset({"MANUAL_REVIEW"}),
}
TERMINAL = frozenset({"FULFILLED", "PAYMENT_FAILED", "EXPIRED", "CANCELED", "REFUNDED", "MANUAL_REVIEW"})
# What commerce-svc must be told after entering a state (settled asynchronously by the worker).
SETTLEMENT_ON: dict[str, str] = {"PAID": "commit", "PAYMENT_FAILED": "release", "EXPIRED": "release"}


class InvalidTransition(Conflict):
    pass


def snapshot(order: Order) -> dict[str, Any]:
    return {"status": order.status, "version": order.version, "total_minor": order.total_minor}


async def load_for_update(session: AsyncSession, order_id: str) -> Order:
    """Row-locks the order: confirm requests and webhook processing on one order serialise here."""
    order = (
        await session.execute(select(Order).where(Order.id == order_id).with_for_update())
    ).scalar_one_or_none()
    if order is None:
        raise NotFound("order_not_found", f"Order {order_id} not found")
    return order


def can_transition(order: Order, to: str) -> bool:
    return to in TRANSITIONS.get(order.status, frozenset())


async def transition(
    session: AsyncSession,
    order: Order,
    to: str,
    *,
    actor_type: str,
    actor_id: str,
    reason: str | None = None,
    settlement: str | None = None,
    **fields: Any,
) -> Order:
    """Moves the (row-locked) order to ``to`` and writes the audit row in the same transaction."""
    if not can_transition(order, to):
        raise InvalidTransition("invalid_transition", f"Order cannot go from {order.status} to {to}")
    before = snapshot(order)
    order.status = to
    order.version += 1
    order.updated_at = datetime.now(UTC)
    if reason:
        order.failure_reason = reason
    owed = settlement or SETTLEMENT_ON.get(to)
    if owed:
        order.settlement = owed
    for name, value in fields.items():
        setattr(order, name, value)
    await session.flush()
    await audit.record(
        session,
        actor_type=actor_type,
        actor_id=actor_id,
        action=f"order.{to.lower()}",
        entity_type="order",
        entity_id=order.id,
        before=before,
        after={**snapshot(order), **({"reason": reason} if reason else {})},
    )
    return order
