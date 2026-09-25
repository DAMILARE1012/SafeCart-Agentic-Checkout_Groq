"""Tells commerce-svc the outcome of each order: commit stock on payment, release it when payment
never happened, void the promotion use when a paid order is refunded.

Order transitions record WHAT is owed in ``orders.settlement`` inside the same transaction. This
worker job delivers it later, with retries, and clears the marker. It works like an outbox, so a
crash between "order paid" and "stock committed" can never lose the commit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from checkout_svc import audit, orders
from checkout_svc.commerce_client import CommerceClient, CommerceRejected, CommerceUnavailable
from checkout_svc.models import OUTBOX, Order
from commerce_common import events
from commerce_common.messaging import enqueue
from commerce_common.money import MoneyDTO

log = structlog.get_logger("checkout.settlement")


async def settle_pending(
    sessions: async_sessionmaker[AsyncSession], commerce: CommerceClient, batch: int = 20
) -> int:
    async with sessions() as session:
        pending = list(
            (
                await session.execute(
                    select(Order.id, Order.settlement)
                    .where(Order.settlement.is_not(None))
                    .order_by(Order.updated_at)
                    .limit(batch)
                )
            ).all()
        )
    settled = 0
    for order_id, action in pending:
        oversold: list[str] = []
        try:
            if action == "commit":
                oversold = (await commerce.commit(order_id)).get("oversold_sku_ids") or []
            elif action == "void":
                await commerce.void(order_id)
            else:
                await commerce.release(order_id)
        except (CommerceUnavailable, CommerceRejected) as exc:
            log.warning("settlement_retry_later", order_id=order_id, action=action, error=type(exc).__name__)
            continue
        async with sessions() as session, session.begin():
            order = await orders.load_for_update(session, order_id)
            if order.settlement == action:
                order.settlement = None
            if oversold and order.status == "PAID":
                # Paid, but the stock was gone (the reservation expired before a very late payment):
                # never ship nothing silently. Compensate: the refund saga takes it from here.
                log.error("order_oversold", order_id=order_id, sku_ids=oversold)
                await orders.transition(
                    session,
                    order,
                    "FULFILLMENT_FAILED",
                    actor_type="system",
                    actor_id="checkout-worker",
                    reason=f"out_of_stock_after_payment: {', '.join(oversold)}"[:120],
                )
        settled += 1
        log.info("order_settled", order_id=order_id, action=action)
    return settled


async def cancel_stale_created(sessions: async_sessionmaker[AsyncSession], older_than_s: int) -> int:
    """Orders stuck in CREATED (the confirm request died mid-way and was never retried): cancel them and
    release anything reserved. A retry that arrives later gets a clear 'order is canceled' answer."""
    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_s)
    async with sessions() as session:
        ids = list(
            (
                await session.execute(
                    select(Order.id).where(Order.status == "CREATED", Order.created_at < cutoff).limit(50)
                )
            ).scalars()
        )
    for order_id in ids:
        async with sessions() as session, session.begin():
            order = await orders.load_for_update(session, order_id)
            if order.status == "CREATED":
                await orders.transition(
                    session,
                    order,
                    "CANCELED",
                    actor_type="system",
                    actor_id="checkout-worker",
                    reason="abandoned_during_confirmation",
                    settlement="release",
                )
    return len(ids)


async def request_fulfillment(
    sessions: async_sessionmaker[AsyncSession], commerce: CommerceClient, batch: int = 20
) -> int:
    """PAID orders whose stock is committed → stage order.paid.v1 (outbox) for fulfillment-svc.

    Runs after settlement so a warehouse never ships stock that commerce-svc hasn't committed. The
    event carries the confirmed quote's lines (the exact items paid for), fetched from commerce-svc.
    """
    async with sessions() as session:
        due = list(
            (
                await session.execute(
                    select(Order.id, Order.quote_id)
                    .where(
                        Order.status == "PAID",
                        Order.settlement.is_(None),
                        Order.fulfillment_requested_at.is_(None),
                    )
                    .order_by(Order.updated_at)
                    .limit(batch)
                )
            ).all()
        )
    requested = 0
    for order_id, quote_id in due:
        try:
            quote = await commerce.get_quote(quote_id)
        except (CommerceUnavailable, CommerceRejected) as exc:
            log.warning("fulfillment_request_retry_later", order_id=order_id, error=type(exc).__name__)
            continue
        lines = [
            events.OrderLine(sku_id=line["sku_id"], name=line["name"], quantity=line["quantity"])
            for line in quote["lines"]
        ]
        async with sessions() as session, session.begin():
            order = await orders.load_for_update(session, order_id)
            if order.status != "PAID" or order.fulfillment_requested_at is not None:
                continue
            await enqueue(
                session,
                OUTBOX,
                events.envelope(
                    events.ORDER_PAID,
                    "checkout-svc",
                    events.OrderPaidV1(
                        order_id=order.id,
                        conversation_id=order.conversation_id,
                        total=MoneyDTO(amount_minor=order.total_minor, currency=order.currency),
                        lines=lines,
                    ),
                ),
            )
            order.fulfillment_requested_at = datetime.now(UTC)
            await audit.record(
                session,
                actor_type="system",
                actor_id="checkout-worker",
                action="fulfillment.requested",
                entity_type="order",
                entity_id=order.id,
                after={"lines": [line.model_dump() for line in lines]},
            )
        requested += 1
        log.info("fulfillment_requested", order_id=order_id)
    return requested
