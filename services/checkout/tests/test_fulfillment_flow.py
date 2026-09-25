"""checkout-svc ↔ fulfillment-svc over RabbitMQ: PAID → order.paid.v1 → fulfillment.*.v1 → final state."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
from checkout_testkit import GATEWAY, confirm, deliver, grant, quoted_cart, session_event
from faststream.rabbit import RabbitQueue, TestRabbitBroker
from sqlalchemy import text

from checkout_svc import settlement, webhooks
from checkout_svc.fulfillment_results import handle_fulfillment_result
from checkout_svc.models import OUTBOX
from checkout_svc.worker import build_broker
from commerce_common import events
from commerce_common.messaging import events_exchange, relay


async def paid_order(checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient) -> dict[str, Any]:
    data = await quoted_cart(commerce_http, "sku_road_air_10_wht", 2)
    g = await grant(checkout, data)
    order_id = (await confirm(checkout, g["token"], data["conversation_id"])).json()["order_id"]
    order = (await checkout.get(f"/v1/orders/{order_id}", headers=GATEWAY)).json()
    await deliver(checkout, session_event("checkout.session.completed", order))
    app = checkout.app  # type: ignore[attr-defined]
    await webhooks.process_pending(app.state.session_factory)
    await settlement.settle_pending(app.state.session_factory, app.state.commerce)
    return {**order, "quote": data["quote"]}


def outcome(event_type: str, order_id: str, **extra: Any) -> events.Envelope:
    model = (
        events.FulfillmentSucceededV1
        if event_type == events.FULFILLMENT_SUCCEEDED
        else events.FulfillmentFailedV1
    )
    return events.envelope(
        event_type,
        "fulfillment-svc",
        model(order_id=order_id, fulfillment_id=f"ful_{uuid.uuid4().hex[:8]}", **extra),
    )


async def status_of(checkout: httpx.AsyncClient, order_id: str) -> str:
    return str((await checkout.get(f"/v1/orders/{order_id}", headers=GATEWAY)).json()["status"])


async def audit_actions(checkout: httpx.AsyncClient, order_id: str) -> list[str]:
    async with checkout.app.state.session_factory() as session:  # type: ignore[attr-defined]
        return list(
            (
                await session.execute(
                    text("SELECT action FROM audit_log WHERE entity_id = :o ORDER BY id"), {"o": order_id}
                )
            ).scalars()
        )


async def test_paid_order_is_handed_to_fulfilment_once_and_completes(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient
) -> None:
    app = checkout.app  # type: ignore[attr-defined]
    sessions = app.state.session_factory
    order = await paid_order(checkout, commerce_http)

    assert await settlement.request_fulfillment(sessions, app.state.commerce) >= 1
    assert await settlement.request_fulfillment(sessions, app.state.commerce) == 0  # staged once, ever
    async with sessions() as session:
        staged = (
            (
                await session.execute(
                    text("SELECT envelope FROM outbox WHERE envelope->'data'->>'order_id' = :o"),
                    {"o": order["id"]},
                )
            )
            .scalars()
            .all()
        )
    assert len(staged) == 1
    paid = events.OrderPaidV1.model_validate(staged[0]["data"])
    assert [(line.sku_id, line.quantity) for line in paid.lines] == [("sku_road_air_10_wht", 2)]
    assert paid.total.model_dump() == order["quote"]["total"]  # the confirmed quote, to the cent

    settings = app.state.settings.model_copy(update={"rabbitmq_url": "amqp://guest:guest@localhost:5672/"})
    broker = build_broker(settings, sessions)
    exchange = events_exchange(settings.events_exchange)
    received: list[events.Envelope] = []

    @broker.subscriber(RabbitQueue("fulfillment.order-paid", routing_key=events.ORDER_PAID), exchange)
    async def fulfillment_stand_in(message: events.Envelope) -> None:
        received.append(message)

    done = outcome(events.FULFILLMENT_SUCCEEDED, order["id"], carrier="MockShip", tracking_number="MS42")
    async with TestRabbitBroker(broker) as br:
        assert await relay(sessions, OUTBOX, br, exchange) >= 1  # order.paid.v1 leaves via the outbox
        assert order["id"] in {m.data["order_id"] for m in received}
        for _ in range(2):  # at-least-once delivery: the same outcome twice
            await br.publish(
                done.model_dump(mode="json"), exchange=exchange, routing_key=done.type, message_id=done.id
            )
        for _ in range(50):
            if await status_of(checkout, order["id"]) == "FULFILLED":
                break
            await asyncio.sleep(0.05)

    assert await status_of(checkout, order["id"]) == "FULFILLED"
    actions = await audit_actions(checkout, order["id"])
    assert actions[-3:] == ["order.paid", "fulfillment.requested", "order.fulfilled"]  # applied exactly once


async def test_fulfilment_failure_is_recorded_for_compensation(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient
) -> None:
    sessions = checkout.app.state.session_factory  # type: ignore[attr-defined]
    order = await paid_order(checkout, commerce_http)

    failed = outcome(events.FULFILLMENT_FAILED, order["id"], reason="warehouse cannot fulfil: sku_x")
    assert await handle_fulfillment_result(sessions, failed) == "FULFILLMENT_FAILED"
    assert await handle_fulfillment_result(sessions, failed) == "duplicate"
    body = (await checkout.get(f"/v1/orders/{order['id']}", headers=GATEWAY)).json()
    assert body["status"] == "FULFILLMENT_FAILED"

    # A late "succeeded" for the same order can't resurrect it: ignored, but kept in the audit trail.
    late = outcome(events.FULFILLMENT_SUCCEEDED, order["id"], carrier="MockShip", tracking_number="MS1")
    assert await handle_fulfillment_result(sessions, late) == "ignored"
    assert (await audit_actions(checkout, order["id"]))[-1] == "event.ignored"
