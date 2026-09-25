"""Stripe webhooks: the ONLY source of truth for payment state (docs P4, §7.3).

receive()      : verify signature → store in inbox (event.id UNIQUE → duplicates are no-ops) → 200 fast
process()      : worker applies each event through the state machine, once. Out-of-order or late events
                 hit an invalid transition and are recorded and ignored; amounts are re-verified.
apply_event()  : the single place a Stripe object changes an order. Reconciliation replays objects it
                 fetched from Stripe through it too, so a missed webhook is fixed by the same rules.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import or_, select
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
    # Compensation refunds (and refunds a human issues in the Stripe Dashboard)
    "refund.created",
    "refund.updated",
    "refund.failed",
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
                    await apply_event(session, event.payload)
                event.processed_at = datetime.now(UTC)
                done += 1
            except Exception as exc:
                event.attempts += 1
                event.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                log.exception("webhook_processing_failed", event_id=event_id, attempts=event.attempts)
    return done


async def apply_event(
    session: AsyncSession, event: dict[str, Any], *, actor_type: str = "webhook", actor_id: str | None = None
) -> None:
    event_type = event["type"]
    if event_type not in HANDLED:
        return
    obj = event["data"]["object"]
    order = await _find_order(session, obj)
    if order is None:
        log.warning("webhook_for_unknown_order", event_type=event_type, object_id=obj.get("id"))
        return
    move = _Mover(session, order, event, actor_type, actor_id or event["id"])

    if event_type.startswith("refund."):
        await _apply_refund(move, order, obj, event_type)
    elif event_type in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        if obj.get("payment_status") != "paid":
            return  # e.g. delayed payment methods: wait for async_payment_succeeded/failed
        if order.status == "PAID":
            return  # duplicate semantic event
        if order.status == "CREATED":
            # Paid before the confirm request recorded its session (e.g. it crashed after creating it):
            # catch the order up instead of dropping a real payment.
            await move(
                "AWAITING_PAYMENT", stripe_checkout_session_id=obj.get("id"), checkout_url=obj.get("url")
            )
        if order.status != "AWAITING_PAYMENT":
            # Money received for an order we consider cancelled/expired/failed: never ignore it.
            await move("MANUAL_REVIEW", reason="payment_for_inactive_order")
            log.error("payment_for_inactive_order", order_id=order.id, status=order.status)
            return
        paid_amount, paid_currency = int(obj.get("amount_total") or -1), str(obj.get("currency", "")).upper()
        if paid_amount != order.total_minor or paid_currency != order.currency:
            # Never trust a mismatch: stop, flag for a human, alert.
            await move("MANUAL_REVIEW", reason="amount_mismatch")
            log.error(
                "payment_amount_mismatch",
                order_id=order.id,
                expected=order.total_minor,
                paid=paid_amount,
                currency=paid_currency,
            )
            return
        await move("PAID", stripe_payment_intent_id=obj.get("payment_intent"))
    elif event_type == "checkout.session.async_payment_failed":
        await move("PAYMENT_FAILED", reason="async_payment_failed")
    elif event_type == "checkout.session.expired":
        await move("EXPIRED", reason="checkout_expired")


async def _apply_refund(move: _Mover, order: Order, refund: dict[str, Any], event_type: str) -> None:
    status = "failed" if event_type == "refund.failed" else str(refund.get("status"))
    fields = {"stripe_refund_id": refund.get("id"), "refund_status": status}
    if order.status == "REFUNDED":
        return  # duplicate semantic event
    if order.status == "FULFILLMENT_FAILED":
        # The refund's webhook beat the transaction that records it (or reconciliation found it): catch up.
        await move("REFUND_PENDING", **fields)
    if status == "succeeded":
        amount, currency = int(refund.get("amount") or -1), str(refund.get("currency", "")).upper()
        if amount != order.total_minor or currency != order.currency:
            await move("MANUAL_REVIEW", reason="refund_amount_mismatch", **fields)  # e.g. a partial refund
            return
        await move("REFUNDED", **fields)
    elif status in {"failed", "canceled"}:
        await move("MANUAL_REVIEW", reason=f"refund_{status}", **fields)
    elif order.status == "REFUND_PENDING":
        order.refund_status = status  # pending / requires_action: progress, not a state change


async def _find_order(session: AsyncSession, obj: dict[str, Any]) -> Order | None:
    order_id = (obj.get("metadata") or {}).get("order_id") or obj.get("client_reference_id")
    stmt = select(Order).with_for_update().limit(1)
    if order_id:
        stmt = stmt.where(Order.id == order_id)
    elif obj.get("object") == "refund":
        # A refund issued by hand in the Dashboard carries no metadata: match it by its payment.
        stmt = stmt.where(
            or_(
                Order.stripe_refund_id == obj.get("id"),
                Order.stripe_payment_intent_id == obj.get("payment_intent"),
            )
        )
    else:
        stmt = stmt.where(Order.stripe_checkout_session_id == obj.get("id"))
    return (await session.execute(stmt)).scalars().first()


class _Mover:
    """Applies one Stripe event's transitions to a row-locked order, attributed to one actor."""

    def __init__(
        self, session: AsyncSession, order: Order, event: dict[str, Any], actor_type: str, actor_id: str
    ) -> None:
        self._session, self._order, self._event = session, order, event
        self._actor_type, self._actor_id = actor_type, actor_id

    async def __call__(self, to: str, *, reason: str | None = None, **fields: Any) -> None:
        order = self._order
        if order.status == to:
            return  # duplicate semantic event (e.g. completed + async_succeeded): already applied
        if not orders.can_transition(order, to):
            # Late / out-of-order event (e.g. "expired" after "paid"): record it, change nothing.
            await audit.record(
                self._session,
                actor_type=self._actor_type,
                actor_id=self._actor_id,
                action="webhook.ignored",
                entity_type="order",
                entity_id=order.id,
                before=orders.snapshot(order),
                after={"event_type": self._event["type"], "would_be": to},
            )
            log.info(
                "webhook_ignored_invalid_transition",
                order_id=order.id,
                status=order.status,
                event=self._event["type"],
            )
            return
        await orders.transition(
            self._session,
            order,
            to,
            actor_type=self._actor_type,
            actor_id=self._actor_id,
            reason=reason,
            **fields,
        )
        log.info("order_transitioned", order_id=order.id, to=to, via=self._event["type"])
