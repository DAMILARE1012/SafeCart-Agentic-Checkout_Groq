"""fulfillment-svc: order.paid.v1 → provider → outcome event, exactly once."""

from __future__ import annotations

import asyncio

from faststream.rabbit import TestRabbitBroker
from fulfillment_testkit import order_paid, outbox_rows, settings
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from commerce_common import events
from commerce_common.events import OrderLine
from commerce_common.messaging import events_exchange
from fulfillment_svc.handler import handle_order_paid
from fulfillment_svc.models import INBOX, FulfillmentJob
from fulfillment_svc.provider import MockProvider, RetryableFulfillmentError, Shipment
from fulfillment_svc.worker import build_broker


async def test_paid_order_is_fulfilled_once(sessions: async_sessionmaker[AsyncSession]) -> None:
    provider = MockProvider(settings())
    message = order_paid("sku_a", "sku_b")
    order_id = message.data["order_id"]

    assert await handle_order_paid(sessions, provider, message) == "succeeded"
    assert await handle_order_paid(sessions, provider, message) == "duplicate"  # redelivery
    again = order_paid(order_id=order_id)  # a different message about the same order
    assert await handle_order_paid(sessions, provider, again) == "duplicate"

    async with sessions() as session:
        job = await session.get(FulfillmentJob, order_id)
        claimed = await session.scalar(select(func.count()).select_from(INBOX))
    assert job is not None and job.status == "succeeded"
    assert job.tracking_number and job.tracking_number.startswith("MS")
    assert claimed and claimed >= 1
    rows = await outbox_rows(sessions, order_id)
    assert [r["event_type"] for r in rows] == [events.FULFILLMENT_SUCCEEDED]  # one outcome, never two
    outcome = events.FulfillmentSucceededV1.model_validate(rows[0]["envelope"]["data"])
    assert outcome.tracking_number == job.tracking_number


async def test_provider_refusal_publishes_failure(sessions: async_sessionmaker[AsyncSession]) -> None:
    provider = MockProvider(settings(fulfillment_mock_fail_skus="sku_gone"))
    message = order_paid("sku_ok", "sku_gone")

    assert await handle_order_paid(sessions, provider, message) == "failed"

    rows = await outbox_rows(sessions, message.data["order_id"])
    assert [r["event_type"] for r in rows] == [events.FULFILLMENT_FAILED]
    assert "sku_gone" in rows[0]["envelope"]["data"]["reason"]


class FlakyProvider:
    """Times out once, then succeeds with the same idempotency key (like a real 3PL)."""

    def __init__(self) -> None:
        self.keys: list[str] = []

    async def submit(self, *, idempotency_key: str, order_id: str, lines: list[OrderLine]) -> Shipment:
        self.keys.append(idempotency_key)
        if len(self.keys) == 1:
            raise RetryableFulfillmentError("timeout")
        return Shipment(carrier="MockShip", tracking_number="MS1")


async def test_transient_provider_errors_are_retried_with_the_same_key(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    provider = FlakyProvider()
    message = order_paid()

    assert await handle_order_paid(sessions, provider, message, max_retries=3) == "succeeded"
    assert provider.keys == [f"fulfil:{message.data['order_id']}"] * 2


async def test_consumer_is_wired_to_order_paid(sessions: async_sessionmaker[AsyncSession]) -> None:
    cfg = settings()
    broker = build_broker(cfg, sessions, MockProvider(cfg))
    message = order_paid()

    async with TestRabbitBroker(broker) as br:
        await br.publish(
            message.model_dump(mode="json"),
            exchange=events_exchange(cfg.events_exchange),
            routing_key=events.ORDER_PAID,
            message_id=message.id,
        )
        for _ in range(50):
            if await outbox_rows(sessions, message.data["order_id"]):
                break
            await asyncio.sleep(0.05)

    rows = await outbox_rows(sessions, message.data["order_id"])
    assert [r["event_type"] for r in rows] == [events.FULFILLMENT_SUCCEEDED]
