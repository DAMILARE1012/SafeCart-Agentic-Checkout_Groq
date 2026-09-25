"""Stripe webhooks: the ONLY source of truth for payment state (docs P4, §7.3).

receive()  : verify signature → store in inbox (event.id UNIQUE → duplicates are no-ops) → 200 fast
process()  : worker applies each event through the state machine, once. Out-of-order or late events
             hit an invalid transition and are recorded and ignored; amounts are re-verified.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from checkout_svc import audit, orders
from checkout_svc.models import Order, WebhookEvent
from checkout_svc.payments import PaymentProvider

log = structlog.get_logger("checkout.webhooks")

MAX_ATTEMPTS = 8
HANDLED = {
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "checkout.session.async_payment_failed",
    "checkout.session.expired",
}


async def receive(
    sessions: async_sessionmaker[AsyncSession],
    payments: PaymentProvider,
    payload: bytes,
    signature: str | None,
) -> bool:
    """True if newly stored, False for a duplicate delivery. Raises InvalidWebhook on a bad signature."""
    event = payments.parse_webhook(payload, signature)
    async with sessions() as session, session.begin():
        inserted = (
            await session.execute(
                insert(WebhookEvent)
                .values(event_id=event["id"], type=event["type"], payload=event, attempts=0)
                .on_conflict_do_nothing(index_elements=[WebhookEvent.event_id])
                .returning(WebhookEvent.event_id)
            )
        ).first()
    log.info("webhook_received", event_type=event["type"], duplicate=inserted is None)
    return inserted is not None


async def process_pending(sessions: async_sessionmaker[AsyncSession], batch: int = 20) -> int:
    """Applies unprocessed events in arrival order. Each event commits (or fails) independently."""
    async with sessions() as session:
        ids = list(
            (
                await session.execute(
                    select(WebhookEvent.event_id)
                    .where(WebhookEvent.processed_at.is_(None), WebhookEvent.attempts < MAX_ATTEMPTS)
                    .order_by(WebhookEvent.received_at)
                    .limit(batch)
                )
            ).scalars()
        )
    done = 0
    for event_id in ids:
        async with sessions() as session, session.begin():
            event = (
                await session.execute(
                    select(WebhookEvent)
                    .where(WebhookEvent.event_id == event_id, WebhookEvent.processed_at.is_(None))
                    .with_for_update(
                        skip_locked=True
                    )  # several worker replicas never process one event twice
                )
            ).scalar_one_or_none()
            if event is None:
                continue
            try:
                async with session.begin_nested():
                    await _apply(session, event.payload)
                event.processed_at = datetime.now(UTC)
                done += 1
            except Exception as exc:
                event.attempts += 1
                event.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                log.exception("webhook_processing_failed", event_id=event_id, attempts=event.attempts)
    return done


async def _apply(session: AsyncSession, event: dict[str, Any]) -> None:
    event_type = event["type"]
    if event_type not in HANDLED:
        return
    obj = event["data"]["object"]
    order = await _find_order(session, obj)
    if order is None:
        log.warning("webhook_for_unknown_order", event_type=event_type, session_id=obj.get("id"))
        return

    if event_type in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        if obj.get("payment_status") != "paid":
            return  # e.g. delayed payment methods: wait for async_payment_succeeded/failed
        if order.status == "PAID":
            return  # duplicate semantic event
        if order.status == "CREATED":
            # Paid before the confirm request recorded its session (e.g. it crashed after creating it):
            # catch the order up instead of dropping a real payment.
            await _move(
                session,
                order,
                "AWAITING_PAYMENT",
                event,
                stripe_checkout_session_id=obj.get("id"),
                checkout_url=obj.get("url"),
            )
        if order.status != "AWAITING_PAYMENT":
            # Money received for an order we consider cancelled/expired/failed: never ignore it.
            await _move(session, order, "MANUAL_REVIEW", event, reason="payment_for_inactive_order")
            log.error("payment_for_inactive_order", order_id=order.id, status=order.status)
            return
        paid_amount, paid_currency = int(obj.get("amount_total") or -1), str(obj.get("currency", "")).upper()
        if paid_amount != order.total_minor or paid_currency != order.currency:
            # Never trust a mismatch: stop, flag for a human, alert.
            await _move(session, order, "MANUAL_REVIEW", event, reason="amount_mismatch")
            log.error(
                "payment_amount_mismatch",
                order_id=order.id,
                expected=order.total_minor,
                paid=paid_amount,
                currency=paid_currency,
            )
            return
        await _move(session, order, "PAID", event, stripe_payment_intent_id=obj.get("payment_intent"))
    elif event_type == "checkout.session.async_payment_failed":
        await _move(session, order, "PAYMENT_FAILED", event, reason="async_payment_failed")
    elif event_type == "checkout.session.expired":
        await _move(session, order, "EXPIRED", event, reason="checkout_expired")


async def _find_order(session: AsyncSession, obj: dict[str, Any]) -> Order | None:
    order_id = (obj.get("metadata") or {}).get("order_id") or obj.get("client_reference_id")
    stmt = select(Order).with_for_update()
    if order_id:
        stmt = stmt.where(Order.id == order_id)
    else:
        stmt = stmt.where(Order.stripe_checkout_session_id == obj.get("id"))
    return (await session.execute(stmt)).scalar_one_or_none()


async def _move(
    session: AsyncSession,
    order: Order,
    to: str,
    event: dict[str, Any],
    *,
    reason: str | None = None,
    **fields: Any,
) -> None:
    if order.status == to:
        return  # duplicate semantic event (e.g. completed + async_succeeded): already applied
    if not orders.can_transition(order, to):
        # Late / out-of-order event (e.g. "expired" after "paid"): record it, change nothing.
        await audit.record(
            session,
            actor_type="webhook",
            actor_id=event["id"],
            action="webhook.ignored",
            entity_type="order",
            entity_id=order.id,
            before=orders.snapshot(order),
            after={"event_type": event["type"], "would_be": to},
        )
        log.info(
            "webhook_ignored_invalid_transition", order_id=order.id, status=order.status, event=event["type"]
        )
        return
    await orders.transition(
        session, order, to, actor_type="webhook", actor_id=event["id"], reason=reason, **fields
    )
    log.info("order_transitioned", order_id=order.id, to=to, via=event["type"])
