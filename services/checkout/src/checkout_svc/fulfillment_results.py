"""fulfillment.succeeded.v1 / fulfillment.failed.v1 → order state (exactly once, via the inbox)."""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from checkout_svc import audit, orders
from checkout_svc.models import INBOX
from commerce_common import events
from commerce_common.messaging import claim

log = structlog.get_logger("checkout.fulfillment")


async def handle_fulfillment_result(
    sessions: async_sessionmaker[AsyncSession], message: events.Envelope
) -> str:
    """Returns 'duplicate', 'ignored' or the new status."""
    async with sessions() as session, session.begin():
        if not await claim(session, INBOX, message.id, message.type):
            return "duplicate"
        order_id = str(message.data["order_id"])
        order = await orders.load_for_update(session, order_id)
        if message.type == events.FULFILLMENT_SUCCEEDED:
            ok = events.FulfillmentSucceededV1.model_validate(message.data)
            target, reason = "FULFILLED", None
            fields = {"fulfillment_reference": f"{ok.carrier} {ok.tracking_number}"}
        elif message.type == events.FULFILLMENT_FAILED:
            failed = events.FulfillmentFailedV1.model_validate(message.data)
            target, reason, fields = "FULFILLMENT_FAILED", failed.reason[:120], {}
        else:
            return "ignored"

        if not orders.can_transition(order, target):
            await audit.record(
                session,
                actor_type="service",
                actor_id="fulfillment-svc",
                action="event.ignored",
                entity_type="order",
                entity_id=order.id,
                before=orders.snapshot(order),
                after={"event_type": message.type, "would_be": target},
            )
            return "ignored"
        await orders.transition(
            session, order, target, actor_type="service", actor_id="fulfillment-svc", reason=reason, **fields
        )
        log.info("order_transitioned", order_id=order.id, to=target, via=message.type)
        return target
