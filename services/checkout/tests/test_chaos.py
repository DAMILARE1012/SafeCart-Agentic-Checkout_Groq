"""Chaos (fault injection) at the exact points where money-moving code can be interrupted (docs §11).

Each test breaks one dependency mid-flow, lets the system retry, and checks that nothing was lost and
nothing happened twice. The live counterpart (``scripts/chaos.py``) does the same against the running
stack by pausing and killing containers.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from checkout_testkit import (
    CHECKOUT_KEY,
    GATEWAY,
    FakeStripe,
    deliver,
    paid_order,
    session_event,
)
from sqlalchemy import func, select, text

from checkout_svc import audit, compensation, settlement, webhooks
from checkout_svc.commerce_client import CommerceClient
from checkout_svc.fulfillment_results import handle_fulfillment_result
from checkout_svc.models import OUTBOX
from checkout_svc.payments import PaymentTemporarilyUnavailable
from commerce_common import events
from commerce_common.messaging import enqueue, events_exchange, relay


def failed(order_id: str) -> events.Envelope:
    return events.envelope(
        events.FULFILLMENT_FAILED,
        "fulfillment-svc",
        events.FulfillmentFailedV1(
            order_id=order_id, fulfillment_id=f"ful_{uuid.uuid4().hex[:8]}", reason="x"
        ),
    )


async def status_of(checkout: httpx.AsyncClient, order_id: str) -> str:
    return str((await checkout.get(f"/v1/orders/{order_id}", headers=GATEWAY)).json()["status"])


async def test_stripe_outage_during_refund_is_retried_with_the_same_key(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient, fake_stripe: FakeStripe
) -> None:
    sessions = checkout.app.state.session_factory  # type: ignore[attr-defined]
    order = await paid_order(checkout, commerce_http, "sku_merino_socks_m")
    await handle_fulfillment_result(sessions, failed(order["id"]))

    fake_stripe.refund_fail_with = PaymentTemporarilyUnavailable  # Stripe 5xx / network down
    for _ in range(3):
        await compensation.refund_failed_orders(sessions, fake_stripe, auto_refund=True)
    assert await status_of(checkout, order["id"]) == "FULFILLMENT_FAILED"  # waiting, not given up

    fake_stripe.refund_fail_with = None  # Stripe is back
    await compensation.refund_failed_orders(sessions, fake_stripe, auto_refund=True)
    assert await status_of(checkout, order["id"]) == "REFUND_PENDING"
    keys = {c["key"] for c in fake_stripe.refund_calls if c["order_id"] == order["id"]}
    assert keys == {f"refund:{order['id']}:full"}  # every attempt used the same key: one refund


async def test_crash_after_stripe_refunded_but_before_we_recorded_it(
    checkout: httpx.AsyncClient,
    commerce_http: httpx.AsyncClient,
    fake_stripe: FakeStripe,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = checkout.app.state.session_factory  # type: ignore[attr-defined]
    order = await paid_order(checkout, commerce_http, "sku_merino_socks_m")
    await handle_fulfillment_result(sessions, failed(order["id"]))

    real_record = audit.record

    async def crash(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("worker killed")  # after Stripe's call returned, before our commit

    monkeypatch.setattr(compensation.audit, "record", crash)
    with pytest.raises(RuntimeError):
        await compensation.refund_failed_orders(sessions, fake_stripe, auto_refund=True)
    assert await status_of(checkout, order["id"]) == "FULFILLMENT_FAILED"  # our transaction rolled back

    monkeypatch.setattr(compensation.audit, "record", real_record)  # the worker restarts
    await compensation.refund_failed_orders(sessions, fake_stripe, auto_refund=True)

    assert await status_of(checkout, order["id"]) == "REFUND_PENDING"
    assert len([c for c in fake_stripe.refund_calls if c["order_id"] == order["id"]]) == 2  # asked twice…
    assert len([r for r in fake_stripe.refunds.values() if r["metadata"]["order_id"] == order["id"]]) == 1
    # …but Stripe's idempotency returned the SAME refund: the customer is refunded exactly once.


async def test_commerce_outage_during_settlement_catches_up(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient, fake_stripe: FakeStripe
) -> None:
    app = checkout.app  # type: ignore[attr-defined]
    down = CommerceClient(
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(503, json={"code": "unavailable"})),
            base_url="http://commerce",
            headers={"Authorization": f"Bearer {CHECKOUT_KEY}"},
        ),
        max_retries=0,
    )
    order = await paid_order(checkout, commerce_http, "sku_merino_socks_m")  # settles normally
    data_order = await paid_order(checkout, commerce_http, "sku_merino_socks_m")
    # Re-owe the commit to simulate the payment landing while commerce-svc is down.
    async with app.state.session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE orders SET settlement = 'commit' WHERE id = :o"), {"o": data_order["id"]}
        )

    await settlement.settle_pending(app.state.session_factory, down)
    await settlement.settle_pending(app.state.session_factory, down)
    async with app.state.session_factory() as session:
        owed = await session.scalar(
            text("SELECT settlement FROM orders WHERE id = :o"), {"o": data_order["id"]}
        )
    assert owed == "commit"  # not lost, not half-applied

    await settlement.settle_pending(app.state.session_factory, app.state.commerce)  # commerce is back
    async with app.state.session_factory() as session:
        owed = await session.scalar(
            text("SELECT settlement FROM orders WHERE id = :o"), {"o": data_order["id"]}
        )
    assert owed is None
    assert await status_of(checkout, order["id"]) == "PAID"


class FlakyBroker:
    """RabbitMQ that refuses publishes until it is 'back'."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.published: list[str] = []

    async def publish(self, message: dict[str, Any], **kwargs: Any) -> None:
        if self.failures:
            self.failures -= 1
            raise ConnectionError("broker unreachable")
        self.published.append(kwargs["message_id"])


async def test_broker_outage_delays_events_but_never_loses_or_duplicates_them(
    checkout: httpx.AsyncClient,
) -> None:
    sessions = checkout.app.state.session_factory  # type: ignore[attr-defined]
    async with sessions() as session, session.begin():
        for n in range(3):
            await enqueue(
                session,
                OUTBOX,
                events.envelope(
                    events.FULFILLMENT_FAILED,
                    "chaos",
                    events.FulfillmentFailedV1(order_id=f"ord_chaos_{n}", fulfillment_id="f", reason="x"),
                ),
            )
    broker = FlakyBroker(failures=2)
    exchange = events_exchange("commerce.events")

    assert await relay(sessions, OUTBOX, broker, exchange) == 0  # type: ignore[arg-type]
    assert await relay(sessions, OUTBOX, broker, exchange) == 0  # type: ignore[arg-type]
    while await relay(sessions, OUTBOX, broker, exchange):  # type: ignore[arg-type]
        pass

    mine = broker.published
    assert len(mine) == len(set(mine))  # nothing published twice
    async with sessions() as session:
        pending = await session.scalar(
            select(func.count()).select_from(OUTBOX).where(OUTBOX.c.published_at.is_(None))
        )
        attempts = await session.scalar(select(func.max(OUTBOX.c.attempts)))
    assert pending == 0  # nothing lost
    assert attempts and attempts >= 1  # the outage was recorded on the row


async def test_a_poisoned_webhook_does_not_block_the_others(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = checkout.app  # type: ignore[attr-defined]
    order = await paid_order(checkout, commerce_http, "sku_merino_socks_m")
    real_apply = webhooks.apply_event
    poison_id: dict[str, str] = {}

    async def flaky_apply(session: Any, event: dict[str, Any], **kwargs: Any) -> None:
        if event["id"] == poison_id.get("id"):
            raise RuntimeError("bug triggered by this event")
        await real_apply(session, event, **kwargs)

    poison = session_event("checkout.session.expired", order, paid=False)
    poison_id["id"] = json.loads(poison)["id"]
    healthy = session_event("checkout.session.completed", order)  # a duplicate delivery of 'paid'
    await deliver(checkout, poison)
    await deliver(checkout, healthy)
    monkeypatch.setattr(webhooks, "apply_event", flaky_apply)

    processed = await webhooks.process_pending(app.state.session_factory)

    assert processed >= 1  # the healthy event went through
    async with app.state.session_factory() as session:
        row = (
            await session.execute(
                text("SELECT attempts, processed_at FROM webhook_inbox WHERE event_id = :e"),
                {"e": poison_id["id"]},
            )
        ).one()
    assert row.attempts == 1 and row.processed_at is None  # parked for retry (and alerting after 8 tries)
    assert await status_of(checkout, order["id"]) == "PAID"
