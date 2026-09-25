"""order.paid.v1 → fulfilment → fulfillment.succeeded.v1 | fulfillment.failed.v1.

Idempotent end to end:
* a job row per order_id (PK) + the inbox → a redelivered order.paid is a no-op;
* the provider is called with idempotency key ``fulfil:{order_id}`` → a retry after a crash
  (provider called, our transaction not yet committed) returns the same shipment;
* the job row, the inbox claim and the outcome event (outbox) commit in ONE transaction.
"""

from __future__ import annotations

import secrets

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from commerce_common import events
from commerce_common.messaging import claim, enqueue
from fulfillment_svc.models import INBOX, OUTBOX, FulfillmentJob
from fulfillment_svc.provider import (
    FulfillmentProvider,
    RetryableFulfillmentError,
    Shipment,
    TerminalFulfillmentError,
)

log = structlog.get_logger("fulfillment")
SOURCE = "fulfillment-svc"


async def handle_order_paid(
    sessions: async_sessionmaker[AsyncSession],
    provider: FulfillmentProvider,
    message: events.Envelope,
    *,
    max_retries: int = 3,
) -> str:
    """Returns 'duplicate', 'succeeded' or 'failed'. Raises on retryable errors (→ message redelivery)."""
    paid = events.OrderPaidV1.model_validate(message.data)
    structlog.contextvars.bind_contextvars(order_id=paid.order_id, event_id=message.id)

    async with sessions() as session:
        if await session.get(FulfillmentJob, paid.order_id) is not None:
            return "duplicate"  # already fulfilled (or failed) for this order: outcome event already staged

    shipment: Shipment | None = None
    failure: str | None = None
    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(max_retries),
            wait=wait_exponential_jitter(initial=0.5, max=5),
            retry=retry_if_exception_type(RetryableFulfillmentError),
            reraise=True,
        ):
            with attempt:
                shipment = await provider.submit(
                    idempotency_key=f"fulfil:{paid.order_id}", order_id=paid.order_id, lines=paid.lines
                )
    except TerminalFulfillmentError as exc:
        failure = str(exc)

    fulfillment_id = f"ful_{secrets.token_hex(10)}"
    async with sessions() as session, session.begin():
        if not await claim(session, INBOX, message.id, message.type):
            return "duplicate"  # a concurrent delivery won the race
        if shipment is not None:
            session.add(
                FulfillmentJob(
                    order_id=paid.order_id,
                    fulfillment_id=fulfillment_id,
                    status="succeeded",
                    lines=[line.model_dump() for line in paid.lines],
                    carrier=shipment.carrier,
                    tracking_number=shipment.tracking_number,
                )
            )
            outcome = events.envelope(
                events.FULFILLMENT_SUCCEEDED,
                SOURCE,
                events.FulfillmentSucceededV1(
                    order_id=paid.order_id,
                    fulfillment_id=fulfillment_id,
                    carrier=shipment.carrier,
                    tracking_number=shipment.tracking_number,
                ),
            )
        else:
            session.add(
                FulfillmentJob(
                    order_id=paid.order_id,
                    fulfillment_id=fulfillment_id,
                    status="failed",
                    lines=[line.model_dump() for line in paid.lines],
                    failure_reason=failure,
                )
            )
            outcome = events.envelope(
                events.FULFILLMENT_FAILED,
                SOURCE,
                events.FulfillmentFailedV1(
                    order_id=paid.order_id, fulfillment_id=fulfillment_id, reason=failure or "unknown"
                ),
            )
        await enqueue(session, OUTBOX, outcome)
    status = "succeeded" if shipment is not None else "failed"
    log.info("fulfillment_" + status, fulfillment_id=fulfillment_id, reason=failure)
    return status
