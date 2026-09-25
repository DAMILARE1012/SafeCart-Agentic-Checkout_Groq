"""Consumer-driven contract: order summaries checkout-svc produces (the widget's order tracker renders
them) must match the widget's published schema, through the whole lifecycle including refunds."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from checkout_testkit import (
    GATEWAY,
    FakeStripe,
    confirm,
    deliver,
    grant,
    paid_order,
    quoted_cart,
    refund_event,
    run_worker,
)
from contract_kit import assert_matches_widget

from checkout_svc.compensation import refund_failed_orders
from checkout_svc.fulfillment_results import handle_fulfillment_result
from commerce_common import events


async def summary(checkout: httpx.AsyncClient, order_id: str) -> dict[str, Any]:
    return (await checkout.get(f"/v1/orders/{order_id}", headers=GATEWAY)).json()


async def test_order_summaries_match_the_widget_through_the_lifecycle(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient, fake_stripe: FakeStripe
) -> None:
    data = await quoted_cart(commerce_http)
    g = await grant(checkout, data)
    response = await confirm(checkout, g["token"], data["conversation_id"])
    assert_matches_widget("ConfirmCheckoutResponse", response.json())
    assert_matches_widget(
        "OrderSummary", await summary(checkout, response.json()["order_id"])
    )  # AWAITING_PAYMENT

    order = await paid_order(checkout, commerce_http, "sku_merino_socks_m")
    assert_matches_widget("OrderSummary", await summary(checkout, order["id"]))  # PAID

    sessions = checkout.app.state.session_factory  # type: ignore[attr-defined]
    await handle_fulfillment_result(
        sessions,
        events.envelope(
            events.FULFILLMENT_FAILED,
            "fulfillment-svc",
            events.FulfillmentFailedV1(
                order_id=order["id"], fulfillment_id=f"ful_{uuid.uuid4().hex[:8]}", reason="x"
            ),
        ),
    )
    await refund_failed_orders(sessions, fake_stripe, auto_refund=True)
    assert_matches_widget("OrderSummary", await summary(checkout, order["id"]))  # REFUND_PENDING + refund
    await deliver(checkout, refund_event("refund.updated", next(iter(fake_stripe.refunds.values()))))
    await run_worker(checkout)
    refunded = await summary(checkout, order["id"])
    assert refunded["status"] == "REFUNDED"
    assert_matches_widget("OrderSummary", refunded)

    missing = await checkout.get("/v1/orders/ord_missing", headers=GATEWAY)
    assert_matches_widget("ApiErrorBody", missing.json())
