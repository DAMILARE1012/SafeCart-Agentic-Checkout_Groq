"""Compensation saga (docs §7.3, P5): a paid order that can't be fulfilled is refunded automatically.

    PAID ─fulfillment.failed / oversold─▶ FULFILLMENT_FAILED ─refund created─▶ REFUND_PENDING
         ─refund webhook (succeeded)─▶ REFUNDED ─settlement 'void'─▶ promotion use given back

``refund_failed_orders`` runs in the checkout worker. For each FULFILLMENT_FAILED order it asks Stripe
for a full refund with the deterministic key ``refund:{order_id}:full``. A retry after a crash, on this
replica or another, gets the SAME refund back from Stripe, never a second one. The refund is written to
the audit log. REFUNDED is only reached when Stripe confirms the refund (webhook, or reconciliation if the
webhook is lost). Refunds Stripe rejects, and auto-refund switched off, go to MANUAL_REVIEW, which
alerts a human.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from checkout_svc import audit, orders
from checkout_svc.models import Order
from checkout_svc.payments import PaymentProvider, PaymentRejected, PaymentTemporarilyUnavailable
from commerce_common.metrics import counter, prime_labels

log = structlog.get_logger("checkout.compensation")

ACTOR = "compensation"
REFUNDS = prime_labels(
    counter("checkout.refunds", "Compensation refunds by outcome"),
    [{"outcome": o} for o in ("created", "rejected", "retry_later")],
)


def refund_idempotency_key(order_id: str) -> str:
    return f"refund:{order_id}:full"


async def refund_failed_orders(
    sessions: async_sessionmaker[AsyncSession],
    payments: PaymentProvider,
    *,
    auto_refund: bool,
    batch: int = 20,
) -> int:
    """Returns the number of orders moved on (refund issued, or handed to a human)."""
    async with sessions() as session:
        due = list(
            (
                await session.execute(
                    select(Order.id, Order.stripe_payment_intent_id, Order.total_minor)
                    .where(Order.status == "FULFILLMENT_FAILED")
                    .order_by(Order.updated_at)
                    .limit(batch)
                )
            ).all()
        )
    moved = 0
    for order_id, payment_intent_id, amount_minor in due:
        if not auto_refund:
            moved += await _to_manual_review(sessions, order_id, "auto_refund_disabled")
            continue
        if not payment_intent_id:
            moved += await _to_manual_review(sessions, order_id, "refund_impossible_no_payment_intent")
            continue
        key = refund_idempotency_key(order_id)
        try:
            refund = await payments.create_refund(
                order_id=order_id,
                payment_intent_id=payment_intent_id,
                amount_minor=amount_minor,
                idempotency_key=key,
            )
        except PaymentTemporarilyUnavailable as exc:
            log.warning("refund_retry_later", order_id=order_id, error=str(exc))
            REFUNDS.add(1, {"outcome": "retry_later"})
            continue  # next tick, same key
        except PaymentRejected as exc:
            log.error("refund_rejected", order_id=order_id, error=str(exc))
            REFUNDS.add(1, {"outcome": "rejected"})
            moved += await _to_manual_review(sessions, order_id, f"refund_rejected: {exc}"[:120])
            continue

        async with sessions() as session, session.begin():
            order = await orders.load_for_update(session, order_id)
            await audit.record(
                session,
                actor_type="system",
                actor_id=ACTOR,
                action="refund.created",
                entity_type="order",
                entity_id=order.id,
                after={
                    "refund_id": refund.refund_id,
                    "amount_minor": refund.amount_minor,
                    "currency": refund.currency,
                    "status": refund.status,
                    "idempotency_key": key,
                    "cause": order.failure_reason,
                },
            )
            if order.status == "FULFILLMENT_FAILED":  # the refund webhook may already have moved it on
                await orders.transition(
                    session,
                    order,
                    "REFUND_PENDING",
                    actor_type="system",
                    actor_id=ACTOR,
                    stripe_refund_id=refund.refund_id,
                    refund_status=refund.status,
                )
        moved += 1
        log.info("refund_created", order_id=order_id, refund_id=refund.refund_id, status=refund.status)
        REFUNDS.add(1, {"outcome": "created"})
    return moved


async def _to_manual_review(sessions: async_sessionmaker[AsyncSession], order_id: str, reason: str) -> int:
    async with sessions() as session, session.begin():
        order = await orders.load_for_update(session, order_id)
        if order.status != "FULFILLMENT_FAILED":
            return 0
        await orders.transition(
            session, order, "MANUAL_REVIEW", actor_type="system", actor_id=ACTOR, reason=reason
        )
    return 1
