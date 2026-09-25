"""Reconciliation against Stripe, and alerting: missed webhooks, mismatches, stuck orders, tampering."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from checkout_testkit import (
    GATEWAY,
    FakeStripe,
    RecordingAlerter,
    confirm,
    grant,
    paid_order,
    quoted_cart,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from checkout_svc.compensation import refund_failed_orders
from checkout_svc.fulfillment_results import handle_fulfillment_result
from checkout_svc.reconciliation import LOCK_KEY, Reconciler, alert_orders_needing_review
from commerce_common import events
from commerce_common.alerts import Alerter


async def order_of(checkout: httpx.AsyncClient, order_id: str) -> dict[str, Any]:
    return (await checkout.get(f"/v1/orders/{order_id}", headers=GATEWAY)).json()


def reconciler(
    checkout: httpx.AsyncClient, stripe: FakeStripe, alerter: Alerter, **kwargs: Any
) -> Reconciler:
    app = checkout.app  # type: ignore[attr-defined]
    return Reconciler(app.state.session_factory, stripe, alerter, app.state.settings, **kwargs)


async def age(checkout: httpx.AsyncClient, order_id: str, minutes: int) -> None:
    """Pretend the order last changed ``minutes`` ago (older than the webhook grace period)."""
    async with checkout.app.state.session_factory() as session, session.begin():  # type: ignore[attr-defined]
        await session.execute(
            text("UPDATE orders SET updated_at = now() - make_interval(mins => :m) WHERE id = :o"),
            {"m": minutes, "o": order_id},
        )


async def audit_rows(checkout: httpx.AsyncClient, order_id: str) -> list[tuple[str, str, str]]:
    async with checkout.app.state.session_factory() as session:  # type: ignore[attr-defined]
        rows = await session.execute(
            text("SELECT action, actor_type, actor_id FROM audit_log WHERE entity_id = :o ORDER BY id"),
            {"o": order_id},
        )
        return [(r.action, r.actor_type, r.actor_id) for r in rows]


async def test_a_payment_whose_webhook_was_lost_is_recovered(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient, fake_stripe: FakeStripe
) -> None:
    data = await quoted_cart(commerce_http)
    g = await grant(checkout, data)
    order_id = (await confirm(checkout, g["token"], data["conversation_id"])).json()["order_id"]
    fake_stripe.pay(order_id)  # the customer paid; the webhook never reached us
    alerter = RecordingAlerter()

    fresh = await reconciler(checkout, fake_stripe, alerter).run()
    assert fresh is not None and fresh.payments_recovered == 0  # young orders are left to the webhooks
    await age(checkout, order_id, 10)
    report = await reconciler(checkout, fake_stripe, alerter).run()

    assert report is not None and report.payments_recovered == 1
    assert (await order_of(checkout, order_id))["status"] == "PAID"
    assert ("order.paid", "system", "reconciler") in await audit_rows(checkout, order_id)
    assert "stripe:missed_webhooks" in alerter.sent_keys()


async def test_a_refund_whose_webhook_was_lost_is_recovered(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient, fake_stripe: FakeStripe
) -> None:
    sessions = checkout.app.state.session_factory  # type: ignore[attr-defined]
    order = await paid_order(checkout, commerce_http, "sku_merino_socks_m")
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
    fake_stripe.refund_status = "pending"
    await refund_failed_orders(sessions, fake_stripe, auto_refund=True)
    next(iter(fake_stripe.refunds.values()))["status"] = "succeeded"  # Stripe finished; webhook lost
    await age(checkout, order["id"], 10)

    report = await reconciler(checkout, fake_stripe, RecordingAlerter()).run()

    assert report is not None and report.refunds_recovered == 1
    assert (await order_of(checkout, order["id"]))["status"] == "REFUNDED"


async def test_an_amount_mismatch_with_stripe_stops_the_order_and_alerts(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient, fake_stripe: FakeStripe
) -> None:
    order = await paid_order(checkout, commerce_http, "sku_merino_socks_m")
    fake_stripe.intents[f"pi_test_{order['id']}"] = {"amount_received": order["total"]["amount_minor"] - 100}
    alerter = RecordingAlerter()

    report = await reconciler(checkout, fake_stripe, alerter).run()

    assert report is not None and report.mismatches == 1
    assert (await order_of(checkout, order["id"]))["status"] == "MANUAL_REVIEW"  # stopped before shipping
    assert ("reconciliation.mismatch", "system", "reconciler") in await audit_rows(checkout, order["id"])
    assert f"order:{order['id']}:reconciliation_mismatch" in alerter.sent_keys()

    # Verified orders are not re-checked on every run.
    again = await reconciler(checkout, fake_stripe, alerter).run()
    assert again is not None and again.mismatches == 0


async def test_a_matching_paid_order_is_verified_once(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient, fake_stripe: FakeStripe
) -> None:
    order = await paid_order(checkout, commerce_http, "sku_merino_socks_m")

    first = await reconciler(checkout, fake_stripe, RecordingAlerter()).run()
    second = await reconciler(checkout, fake_stripe, RecordingAlerter()).run()

    assert first is not None and first.orders_verified >= 1
    assert second is not None and second.orders_verified == 0
    assert (await order_of(checkout, order["id"]))["status"] == "PAID"


async def test_stuck_orders_and_dead_letters_raise_alerts(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient, fake_stripe: FakeStripe
) -> None:
    order = await paid_order(checkout, commerce_http, "sku_merino_socks_m")
    await age(checkout, order["id"], 60)  # paid an hour ago, warehouse never answered
    alerter = RecordingAlerter()

    async def three_dead_letters() -> int:
        return 3

    report = await reconciler(checkout, fake_stripe, alerter, dead_letter_depth=three_dead_letters).run()

    assert report is not None and report.dead_letters == 3
    assert f"order:{order['id']}:fulfillment_stuck" in alerter.sent_keys()
    assert "events:dead_letters" in alerter.sent_keys()


async def test_a_tampered_audit_log_is_detected(
    checkout: httpx.AsyncClient, databases: dict[str, str], fake_stripe: FakeStripe
) -> None:
    # Only a superuser bypassing the append-only trigger can do this; the hash chain still notices.
    engine = create_async_engine(databases["checkout"], isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT id, action FROM audit_log ORDER BY id LIMIT 1"))).one()
        await conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only"))
        try:
            await conn.execute(
                text("UPDATE audit_log SET action = 'order.tampered' WHERE id = :i"), {"i": row.id}
            )
            alerter = RecordingAlerter()
            report = await reconciler(checkout, fake_stripe, alerter).run()
            assert report is not None and report.audit_chain_ok is False
            assert "audit:chain_broken" in alerter.sent_keys()
        finally:
            await conn.execute(
                text("UPDATE audit_log SET action = :a WHERE id = :i"), {"a": row.action, "i": row.id}
            )
            await conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_append_only"))
    await engine.dispose()


async def test_only_one_replica_reconciles_at_a_time(
    checkout: httpx.AsyncClient, fake_stripe: FakeStripe
) -> None:
    sessions: async_sessionmaker[AsyncSession] = checkout.app.state.session_factory  # type: ignore[attr-defined]
    async with sessions() as other_replica:
        await other_replica.execute(text("SELECT pg_advisory_lock(:k)"), {"k": LOCK_KEY})
        assert await reconciler(checkout, fake_stripe, RecordingAlerter()).run() is None
        await other_replica.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})


@pytest.mark.parametrize("webhook_status", [500, 200])
async def test_a_review_alert_is_marked_sent_only_once_delivered(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient, webhook_status: int
) -> None:
    sessions = checkout.app.state.session_factory  # type: ignore[attr-defined]
    order = await paid_order(checkout, commerce_http, "sku_merino_socks_m")
    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE orders SET status = 'MANUAL_REVIEW', attention_alerted_at = NULL WHERE id = :o"),
            {"o": order["id"]},
        )
    posted: list[dict[str, Any]] = []

    def slack(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(webhook_status)

    alerter = Alerter(
        "https://hooks.example.test/alerts",
        source="checkout-svc",
        http=httpx.AsyncClient(transport=httpx.MockTransport(slack)),
    )
    await alert_orders_needing_review(sessions, alerter)

    async with sessions() as session:
        marked = await session.scalar(
            text("SELECT attention_alerted_at IS NOT NULL FROM orders WHERE id = :o"), {"o": order["id"]}
        )
    assert marked is (webhook_status == 200)  # a failed delivery is retried next tick
    mine = [p for p in posted if p["alert"].get("order_id") == order["id"]]
    assert mine and mine[0]["text"].startswith("[CRITICAL] checkout-svc: Order needs manual review")
    # leave no unalerted review orders behind for other tests
    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE orders SET attention_alerted_at = now() WHERE status = 'MANUAL_REVIEW'")
        )
    await alerter.aclose()
