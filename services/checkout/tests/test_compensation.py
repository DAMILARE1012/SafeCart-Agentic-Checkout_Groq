"""Compensation saga: a paid order that can't be fulfilled is refunded automatically, exactly once."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from checkout_testkit import (
    GATEWAY,
    FakeStripe,
    RecordingAlerter,
    confirm,
    deliver,
    grant,
    paid_order,
    quoted_cart,
    refund_event,
    refunds_for,
    run_worker,
    session_event,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkout_svc.audit import verify_chain
from checkout_svc.compensation import refund_failed_orders
from checkout_svc.fulfillment_results import handle_fulfillment_result
from checkout_svc.payments import PaymentRejected
from checkout_svc.reconciliation import alert_orders_needing_review
from commerce_common import events


def failed(order_id: str, reason: str = "warehouse cannot fulfil: sku_x") -> events.Envelope:
    return events.envelope(
        events.FULFILLMENT_FAILED,
        "fulfillment-svc",
        events.FulfillmentFailedV1(
            order_id=order_id, fulfillment_id=f"ful_{uuid.uuid4().hex[:8]}", reason=reason
        ),
    )


async def order_of(checkout: httpx.AsyncClient, order_id: str) -> dict[str, Any]:
    return (await checkout.get(f"/v1/orders/{order_id}", headers=GATEWAY)).json()


async def audit_actions(checkout: httpx.AsyncClient, order_id: str) -> list[str]:
    async with checkout.app.state.session_factory() as session:  # type: ignore[attr-defined]
        return list(
            (
                await session.execute(
                    text("SELECT action FROM audit_log WHERE entity_id = :o ORDER BY id"), {"o": order_id}
                )
            ).scalars()
        )


async def commerce_scalar(databases: dict[str, str], sql: str, **params: Any) -> Any:
    engine = create_async_engine(databases["commerce"])
    async with engine.connect() as conn:
        value = (await conn.execute(text(sql), params)).scalar()
    await engine.dispose()
    return value


async def test_failed_fulfilment_is_refunded_once_audited_and_the_promo_given_back(
    checkout: httpx.AsyncClient,
    commerce_http: httpx.AsyncClient,
    fake_stripe: FakeStripe,
    databases: dict[str, str],
) -> None:
    app = checkout.app  # type: ignore[attr-defined]
    sessions = app.state.session_factory
    order = await paid_order(checkout, commerce_http, "sku_road_air_08_wht", promo="WELCOME10")
    promo_uses = await commerce_scalar(
        databases, "SELECT redemption_count FROM promotions WHERE code = 'WELCOME10'"
    )

    assert await handle_fulfillment_result(sessions, failed(order["id"])) == "FULFILLMENT_FAILED"
    assert (await order_of(checkout, order["id"]))["status"] == "FULFILLMENT_FAILED"

    # The worker issues the refund; running it again (a retry, a second replica) never refunds twice.
    await refund_failed_orders(sessions, fake_stripe, auto_refund=True)
    await refund_failed_orders(sessions, fake_stripe, auto_refund=True)
    calls = refunds_for(fake_stripe, order["id"])
    assert len(calls) == 1
    call = calls[0]
    assert call["key"] == f"refund:{order['id']}:full"
    assert call["amount"] == order["total"]["amount_minor"]  # full refund, to the cent
    assert call["payment_intent"] == f"pi_test_{order['id']}"

    body = await order_of(checkout, order["id"])
    assert body["status"] == "REFUND_PENDING"  # REFUNDED only when Stripe confirms it
    assert body["refund"]["amount"] == order["total"]

    # Stripe confirms, twice (at-least-once delivery).
    refund = next(iter(fake_stripe.refunds.values()))
    payload = refund_event("refund.updated", refund, status="succeeded")
    assert (await deliver(checkout, payload)).json()["duplicate"] is False
    assert (await deliver(checkout, payload)).json()["duplicate"] is True
    await run_worker(checkout)  # applies the webhook, then settles the 'void' with commerce-svc

    body = await order_of(checkout, order["id"])
    assert body["status"] == "REFUNDED"
    assert body["refund"]["status"] == "succeeded"
    status = await commerce_scalar(
        databases, "SELECT status FROM promotion_redemptions WHERE order_id = :o", o=order["id"]
    )
    assert status == "voided"
    after = await commerce_scalar(
        databases, "SELECT redemption_count FROM promotions WHERE code = 'WELCOME10'"
    )
    assert after == promo_uses - 1

    actions = await audit_actions(checkout, order["id"])
    assert actions[-4:] == [
        "order.fulfillment_failed",
        "refund.created",
        "order.refund_pending",
        "order.refunded",
    ]
    async with sessions() as session:
        ok, broken = await verify_chain(session)
    assert ok, f"audit chain broken at {broken}"


async def test_refund_webhook_that_arrives_first_is_caught_up(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient, fake_stripe: FakeStripe
) -> None:
    sessions = checkout.app.state.session_factory  # type: ignore[attr-defined]
    order = await paid_order(checkout, commerce_http, "sku_road_air_08_wht")
    await handle_fulfillment_result(sessions, failed(order["id"]))

    # Stripe created the refund and its webhook landed before our own transaction recorded it.
    await fake_stripe.create_refund(
        order_id=order["id"],
        payment_intent_id=f"pi_test_{order['id']}",
        amount_minor=order["total"]["amount_minor"],
        idempotency_key=f"refund:{order['id']}:full",
    )
    await deliver(checkout, refund_event("refund.created", next(iter(fake_stripe.refunds.values()))))
    await run_worker(checkout)

    assert (await order_of(checkout, order["id"]))["status"] == "REFUNDED"
    await refund_failed_orders(sessions, fake_stripe, auto_refund=True)
    assert len(refunds_for(fake_stripe, order["id"])) == 1  # the worker made no second request
    actions = await audit_actions(checkout, order["id"])
    assert actions[-3:] == ["order.fulfillment_failed", "order.refund_pending", "order.refunded"]


async def test_a_refund_stripe_rejects_goes_to_a_human_who_is_alerted_once(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient, fake_stripe: FakeStripe
) -> None:
    sessions = checkout.app.state.session_factory  # type: ignore[attr-defined]
    order = await paid_order(checkout, commerce_http, "sku_road_air_08_wht")
    await handle_fulfillment_result(sessions, failed(order["id"]))
    fake_stripe.refund_fail_with = PaymentRejected

    await refund_failed_orders(sessions, fake_stripe, auto_refund=True)

    assert (await order_of(checkout, order["id"]))["status"] == "MANUAL_REVIEW"
    alerter = RecordingAlerter()
    await alert_orders_needing_review(sessions, alerter)
    await alert_orders_needing_review(sessions, alerter)  # next tick: already alerted
    assert alerter.sent_keys().count(f"order:{order['id']}:manual_review") == 1


async def test_refund_failing_at_stripe_goes_to_manual_review(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient, fake_stripe: FakeStripe
) -> None:
    sessions = checkout.app.state.session_factory  # type: ignore[attr-defined]
    order = await paid_order(checkout, commerce_http, "sku_road_air_08_wht")
    await handle_fulfillment_result(sessions, failed(order["id"]))
    fake_stripe.refund_status = "pending"
    await refund_failed_orders(sessions, fake_stripe, auto_refund=True)

    refund = next(iter(fake_stripe.refunds.values()))
    await deliver(checkout, refund_event("refund.failed", refund, status="failed"))
    await run_worker(checkout)

    body = await order_of(checkout, order["id"])
    assert body["status"] == "MANUAL_REVIEW"
    assert body["refund"]["status"] == "failed"


async def test_auto_refund_switched_off_hands_the_order_to_a_human(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient, fake_stripe: FakeStripe
) -> None:
    sessions = checkout.app.state.session_factory  # type: ignore[attr-defined]
    order = await paid_order(checkout, commerce_http, "sku_road_air_08_wht")
    await handle_fulfillment_result(sessions, failed(order["id"]))

    await refund_failed_orders(sessions, fake_stripe, auto_refund=False)

    assert refunds_for(fake_stripe, order["id"]) == []
    assert (await order_of(checkout, order["id"]))["status"] == "MANUAL_REVIEW"


async def test_payment_for_stock_that_is_gone_is_compensated(
    checkout: httpx.AsyncClient,
    commerce_http: httpx.AsyncClient,
    fake_stripe: FakeStripe,
    databases: dict[str, str],
) -> None:
    sessions = checkout.app.state.session_factory  # type: ignore[attr-defined]
    data = await quoted_cart(commerce_http, "sku_trail_gtx_09_blk", 1)
    g = await grant(checkout, data)
    order = await order_of(
        checkout, (await confirm(checkout, g["token"], data["conversation_id"])).json()["order_id"]
    )
    # The reservation lapsed and the last unit sold to someone else before this (very late) payment.
    engine = create_async_engine(databases["commerce"])
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE inventory_reservations SET status = 'released' WHERE order_id = :o"),
            {"o": order["id"]},
        )
        await conn.execute(
            text(
                "UPDATE inventory SET reserved = reserved - 1, on_hand = reserved - 1 "
                "WHERE sku_id = 'sku_trail_gtx_09_blk'"
            )
        )
    await engine.dispose()

    await deliver(checkout, session_event("checkout.session.completed", order))
    await run_worker(checkout)  # PAID → commit reports the SKU oversold → compensation starts

    body = await order_of(checkout, order["id"])
    assert body["status"] == "FULFILLMENT_FAILED"
    await refund_failed_orders(sessions, fake_stripe, auto_refund=True)
    assert (await order_of(checkout, order["id"]))["status"] == "REFUND_PENDING"
    assert len(refunds_for(fake_stripe, order["id"])) == 1
