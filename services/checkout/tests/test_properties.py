"""Property-based tests of the money invariants (docs §11), model-based: hypothesis generates webhook
histories (duplicates, reorderings, wrong amounts, refund outcomes) and a tiny reference model says what
the order state must be. The real system (HTTP webhook intake → inbox → worker → Postgres) must agree.

Invariants:
- Replaying webhooks any number of times, in any order, ends where the reference model says.
- Money is never lost: once a correct payment is seen, the order ends PAID or MANUAL_REVIEW.
- Every FULFILLMENT_FAILED order ends REFUNDED or MANUAL_REVIEW (or REFUND_PENDING while Stripe is
  still processing), and is refunded at most once however often the job runs.
- No payment exists without a redeemed confirmation.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from checkout_testkit import (
    GATEWAY,
    FakeStripe,
    confirm,
    deliver,
    grant,
    quoted_cart,
    run_worker,
    session_event,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkout_svc.compensation import refund_failed_orders
from checkout_svc.fulfillment_results import handle_fulfillment_result
from commerce_common import events

SKU = "sku_merino_socks_l"
PROPERTY_SETTINGS = settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)

PAYMENT_EVENTS = ["paid", "paid_wrong_amount", "expired", "async_failed"]
INACTIVE = {"EXPIRED", "PAYMENT_FAILED", "CANCELED"}


def reference_payment_state(history: list[str]) -> str:
    """The order state machine, as a spec: what each webhook must do from each state."""
    state = "AWAITING_PAYMENT"
    for kind in history:
        if kind == "paid":
            state = {"AWAITING_PAYMENT": "PAID", **dict.fromkeys(INACTIVE, "MANUAL_REVIEW")}.get(state, state)
        elif kind == "paid_wrong_amount":
            state = {"AWAITING_PAYMENT": "MANUAL_REVIEW", **dict.fromkeys(INACTIVE, "MANUAL_REVIEW")}.get(
                state, state
            )
        elif kind == "expired" and state == "AWAITING_PAYMENT":
            state = "EXPIRED"
        elif kind == "async_failed" and state == "AWAITING_PAYMENT":
            state = "PAYMENT_FAILED"
    return state


def reference_refund_state(outcomes: list[str]) -> str:
    state = "REFUND_PENDING"  # after the compensation job issued the refund
    for outcome in outcomes:
        if outcome == "succeeded" and state in {"REFUND_PENDING", "MANUAL_REVIEW"}:
            state = "REFUNDED"
        elif outcome == "failed" and state == "REFUND_PENDING":
            state = "MANUAL_REVIEW"
    return state


@pytest.fixture(scope="module", autouse=True)
async def plenty_of_stock(databases: dict[str, str]) -> None:
    engine = create_async_engine(databases["commerce"])
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE inventory SET on_hand = reserved + 10000 WHERE sku_id = :s"), {"s": SKU}
        )
    await engine.dispose()


async def new_order(checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient) -> dict[str, Any]:
    data = await quoted_cart(commerce_http, SKU)
    g = await grant(checkout, data)
    order_id = (await confirm(checkout, g["token"], data["conversation_id"])).json()["order_id"]
    return (await checkout.get(f"/v1/orders/{order_id}", headers=GATEWAY)).json()


def payload_for(kind: str, order: dict[str, Any]) -> bytes:
    if kind == "paid":
        return session_event("checkout.session.completed", order)
    if kind == "paid_wrong_amount":
        return session_event("checkout.session.completed", order, amount=order["total"]["amount_minor"] - 1)
    if kind == "expired":
        return session_event("checkout.session.expired", order, paid=False)
    return session_event("checkout.session.async_payment_failed", order, paid=False)


@PROPERTY_SETTINGS
@given(
    history=st.lists(st.sampled_from(PAYMENT_EVENTS), min_size=1, max_size=6),
    redeliveries=st.lists(st.integers(min_value=0, max_value=5), max_size=4),
)
async def test_any_webhook_history_ends_where_the_state_machine_says(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient, history: list[str], redeliveries: list[int]
) -> None:
    order = await new_order(checkout, commerce_http)
    sent: list[bytes] = []
    for kind in history:  # each is a distinct Stripe event (new id), delivered in this order
        sent.append(payload_for(kind, order))
        await deliver(checkout, sent[-1])
    for index in redeliveries:  # Stripe redelivers some of them later: exact duplicates
        await deliver(checkout, sent[index % len(sent)])
    await run_worker(checkout)
    await run_worker(checkout)  # a second pass changes nothing

    status = (await checkout.get(f"/v1/orders/{order['id']}", headers=GATEWAY)).json()["status"]
    assert status == reference_payment_state(history), history
    # Every event must be APPLIED (or recorded as ignored), never left failing in the inbox: a late
    # event that crashed its handler would retry until dead-lettered (found live via the dashboards).
    async with checkout.app.state.session_factory() as session:  # type: ignore[attr-defined]
        stuck = await session.scalar(
            text(
                "SELECT count(*) FROM webhook_inbox WHERE processed_at IS NULL "
                "AND payload->'data'->'object'->>'client_reference_id' = :o"
            ),
            {"o": order["id"]},
        )
    assert stuck == 0, f"{stuck} webhook(s) failed to process for history {history}"
    if "paid" in history:
        assert status in {"PAID", "MANUAL_REVIEW"}, "a real payment must never be dropped"


@PROPERTY_SETTINGS
@given(
    outcomes=st.lists(st.sampled_from(["pending", "succeeded", "failed"]), max_size=4),
    job_runs=st.integers(min_value=1, max_value=4),
)
async def test_every_failed_fulfilment_is_refunded_once_or_handed_to_a_human(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient, outcomes: list[str], job_runs: int
) -> None:
    stripe = FakeStripe()
    checkout.app.state.payments = stripe  # type: ignore[attr-defined]
    order = await new_order(checkout, commerce_http)
    stripe.sessions.setdefault(f"cs_test_{order['id']}", {"currency": "usd", "amount_total": 0})
    await deliver(checkout, session_event("checkout.session.completed", order))
    await run_worker(checkout)
    sessions = checkout.app.state.session_factory  # type: ignore[attr-defined]
    failed = events.envelope(
        events.FULFILLMENT_FAILED,
        "fulfillment-svc",
        events.FulfillmentFailedV1(
            order_id=order["id"], fulfillment_id=f"ful_{uuid.uuid4().hex[:8]}", reason="x"
        ),
    )
    await handle_fulfillment_result(sessions, failed)

    stripe.refund_status = "pending"
    for _ in range(job_runs):  # retries, several replicas, restarts
        await refund_failed_orders(sessions, stripe, auto_refund=True)
    refund = next(iter(stripe.refunds.values()))
    for outcome in outcomes:
        event = {
            "id": f"evt_{uuid.uuid4().hex[:16]}",
            "type": "refund.failed" if outcome == "failed" else "refund.updated",
            "data": {"object": {**refund, "status": outcome}},
        }
        await deliver(checkout, json.dumps(event).encode())
    await run_worker(checkout)

    status = (await checkout.get(f"/v1/orders/{order['id']}", headers=GATEWAY)).json()["status"]
    assert len([c for c in stripe.refund_calls if c["order_id"] == order["id"]]) >= 1
    assert len({c["key"] for c in stripe.refund_calls if c["order_id"] == order["id"]}) == 1  # one refund
    assert status == reference_refund_state(outcomes), outcomes
    assert status in {"REFUND_PENDING", "REFUNDED", "MANUAL_REVIEW"}


async def test_no_payment_exists_without_a_redeemed_confirmation(checkout: httpx.AsyncClient) -> None:
    """Runs after the property tests have created many orders: checks every one of them."""
    async with checkout.app.state.session_factory() as session:  # type: ignore[attr-defined]
        orphans = await session.scalar(
            text(
                "SELECT count(*) FROM orders o LEFT JOIN confirmations c ON c.id = o.confirmation_id "
                "WHERE o.status NOT IN ('CREATED', 'CANCELED') AND (c.id IS NULL OR c.used_at IS NULL)"
            )
        )
    assert orphans == 0
