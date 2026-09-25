"""checkout-svc: explicit confirmation → order → (fake) Stripe → signed webhooks → settlement → audit."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from checkout_testkit import (
    AGENT,
    GATEWAY,
    FakeStripe,
    confirm,
    deliver,
    grant,
    quoted_cart,
    session_event,
    sign,
)
from sqlalchemy import text

from checkout_svc import settlement, webhooks
from checkout_svc.audit import verify_chain
from checkout_svc.payments import PaymentRejected, PaymentTemporarilyUnavailable
from checkout_svc.settings import CheckoutSettings


async def order_of(checkout: httpx.AsyncClient, order_id: str) -> dict[str, Any]:
    return (await checkout.get(f"/v1/orders/{order_id}", headers=GATEWAY)).json()


async def run_worker(checkout: httpx.AsyncClient) -> None:
    app = checkout.app  # type: ignore[attr-defined]
    await webhooks.process_pending(app.state.session_factory)
    await settlement.settle_pending(app.state.session_factory, app.state.commerce)


async def test_happy_path_confirm_pay_settle(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient, fake_stripe: FakeStripe
) -> None:
    data = await quoted_cart(commerce_http, "sku_road_air_10_wht")
    g = await grant(checkout, data)

    first = await confirm(checkout, g["token"], data["conversation_id"], key="click-1")
    replay = await confirm(
        checkout, g["token"], data["conversation_id"], key="click-1"
    )  # double click / retry
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert len(fake_stripe.calls) == 1  # the retry never reached Stripe again
    call = fake_stripe.calls[0]
    assert call["idempotency_key"] == f"pay:{first.json()['order_id']}:v1"
    assert call["quote"]["total"] == data["quote"]["total"]  # charge == the confirmed quote, to the cent

    order = await order_of(checkout, first.json()["order_id"])
    assert order["status"] == "AWAITING_PAYMENT"
    assert order["checkout_url"].startswith("https://checkout.stripe.test/")
    locked = (await commerce_http.get(f"/v1/quotes/{data['quote']['id']}", headers=AGENT)).json()
    assert locked["status"] == "locked"

    # Stripe delivers "completed" twice (at-least-once delivery)
    payload = session_event("checkout.session.completed", order)
    assert (await deliver(checkout, payload)).json() == {"received": True, "duplicate": False}
    assert (await deliver(checkout, payload)).json() == {"received": True, "duplicate": True}
    await run_worker(checkout)

    order = await order_of(checkout, order["id"])
    assert order["status"] == "PAID"
    assert order["checkout_url"] is None
    cart = (await commerce_http.get(f"/v1/carts/{data['cart']['id']}", headers=AGENT)).json()
    assert cart["lines"] == []  # stock committed, cart emptied

    async with checkout.app.state.session_factory() as session:  # type: ignore[attr-defined]
        ok, broken = await verify_chain(session)
        actions = list(
            (
                await session.execute(
                    text("SELECT action FROM audit_log WHERE entity_id = :o ORDER BY id"), {"o": order["id"]}
                )
            ).scalars()
        )
    assert ok, f"audit chain broken at {broken}"
    assert actions == ["order.created", "order.awaiting_payment", "order.paid"]


async def test_grant_is_single_use_and_bound_to_its_conversation(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient
) -> None:
    data = await quoted_cart(commerce_http)
    g = await grant(checkout, data)
    stranger = await confirm(checkout, g["token"], "web_" + "0" * 32)
    assert stranger.status_code == 403

    assert (await confirm(checkout, g["token"], data["conversation_id"], key="a")).status_code == 200
    reused = await confirm(
        checkout, g["token"], data["conversation_id"], key="b"
    )  # different click, same grant
    assert reused.status_code == 409
    assert reused.json()["code"] == "confirmation_used"

    forged = await confirm(checkout, "not-a-real-token-" + "x" * 20, data["conversation_id"])
    assert forged.status_code == 404


async def test_expired_grant_is_refused(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient
) -> None:
    data = await quoted_cart(commerce_http)
    g = await grant(checkout, data)
    async with checkout.app.state.session_factory() as session, session.begin():  # type: ignore[attr-defined]
        await session.execute(
            text("UPDATE confirmations SET expires_at = now() - interval '1 second' WHERE quote_id = :q"),
            {"q": data["quote"]["id"]},
        )
    r = await confirm(checkout, g["token"], data["conversation_id"])
    assert r.status_code == 410
    assert r.json()["code"] == "confirmation_expired"


async def test_grants_are_only_issued_for_the_owners_valid_quote(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient
) -> None:
    data = await quoted_cart(commerce_http)
    other_conversation = await checkout.post(
        "/internal/confirmations",
        json={"quote_id": data["quote"]["id"], "conversation_id": "web_" + "1" * 32},
        headers=GATEWAY,
    )
    assert other_conversation.status_code == 403


async def test_agent_can_read_orders_but_never_confirm(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient
) -> None:
    data = await quoted_cart(commerce_http)
    g = await grant(checkout, data)
    r = await checkout.post(
        "/v1/checkout/confirm",
        json={"confirmation_token": g["token"], "conversation_id": data["conversation_id"]},
        headers={**AGENT, "Idempotency-Key": "k"},
    )
    assert r.status_code == 403
    grant_attempt = await checkout.post(
        "/internal/confirmations",
        json={"quote_id": data["quote"]["id"], "conversation_id": data["conversation_id"]},
        headers=AGENT,
    )
    assert grant_attempt.status_code == 403
    listing = await checkout.get(f"/v1/conversations/{data['conversation_id']}/orders", headers=AGENT)
    assert listing.status_code == 200


async def test_cart_change_after_grant_cancels_cleanly(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient, fake_stripe: FakeStripe
) -> None:
    data = await quoted_cart(commerce_http)
    g = await grant(checkout, data)
    await commerce_http.post(
        f"/v1/carts/{data['cart']['id']}/items",
        json={"sku_id": "sku_merino_socks_l", "quantity": 1},
        headers={**AGENT, "Idempotency-Key": "extra-item"},
    )
    r = await confirm(checkout, g["token"], data["conversation_id"])
    assert r.status_code == 409
    assert r.json()["code"] == "quote_invalid"
    assert fake_stripe.calls == []  # nothing was sent to Stripe


async def test_webhook_signature_is_required(checkout: httpx.AsyncClient) -> None:
    payload = b'{"id":"evt_x","type":"checkout.session.completed","data":{"object":{}}}'
    assert (
        await deliver(checkout, payload, signature=sign(payload, secret="whsec_wrong_" + "x" * 20))
    ).status_code == 400
    stale = sign(payload, timestamp=1_600_000_000)  # replayed old event
    assert (await deliver(checkout, payload, signature=stale)).status_code == 400


async def test_amount_mismatch_goes_to_manual_review(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient
) -> None:
    data = await quoted_cart(commerce_http)
    g = await grant(checkout, data)
    order = await order_of(
        checkout, (await confirm(checkout, g["token"], data["conversation_id"])).json()["order_id"]
    )
    await deliver(checkout, session_event("checkout.session.completed", order, amount=1))
    await run_worker(checkout)
    assert (await order_of(checkout, order["id"]))["status"] == "MANUAL_REVIEW"


async def test_expiry_releases_stock_and_a_late_payment_is_never_ignored(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient
) -> None:
    data = await quoted_cart(commerce_http, "sku_storm_shell_m_navy", 7)  # every unit in stock
    g = await grant(checkout, data)
    order = await order_of(
        checkout, (await confirm(checkout, g["token"], data["conversation_id"])).json()["order_id"]
    )
    product = (await commerce_http.get("/v1/products/prod_storm_shell", headers=AGENT)).json()
    assert product["in_stock"] is False  # reserved

    await deliver(checkout, session_event("checkout.session.expired", order, paid=False))
    await run_worker(checkout)
    assert (await order_of(checkout, order["id"]))["status"] == "EXPIRED"
    product = (await commerce_http.get("/v1/products/prod_storm_shell", headers=AGENT)).json()
    assert product["in_stock"] is True  # released

    await deliver(checkout, session_event("checkout.session.completed", order))  # money arrives anyway
    await run_worker(checkout)
    assert (await order_of(checkout, order["id"]))["status"] == "MANUAL_REVIEW"


async def test_payment_while_order_still_created_is_caught_up(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient, fake_stripe: FakeStripe
) -> None:
    data = await quoted_cart(commerce_http)
    g = await grant(checkout, data)
    fake_stripe.fail_with = PaymentTemporarilyUnavailable  # confirm dies after creating the order
    r = await confirm(checkout, g["token"], data["conversation_id"], key="crashy")
    assert r.status_code == 503
    assert r.json()["retryable"] is True
    orders = (
        await checkout.get(f"/v1/conversations/{data['conversation_id']}/orders", headers=GATEWAY)
    ).json()
    order = orders["orders"][0]
    assert order["status"] == "CREATED"

    await deliver(checkout, session_event("checkout.session.completed", order))
    await run_worker(checkout)
    assert (await order_of(checkout, order["id"]))["status"] == "PAID"


async def test_stripe_rejection_cancels_and_releases(
    checkout: httpx.AsyncClient, commerce_http: httpx.AsyncClient, fake_stripe: FakeStripe
) -> None:
    data = await quoted_cart(commerce_http, "sku_summit_ltd_10_org", 1)
    g = await grant(checkout, data)
    fake_stripe.fail_with = PaymentRejected
    r = await confirm(checkout, g["token"], data["conversation_id"])
    assert r.status_code == 409
    assert r.json()["code"] == "payment_unavailable"
    await run_worker(checkout)
    quote = (await commerce_http.get(f"/v1/quotes/{data['quote']['id']}", headers=AGENT)).json()
    assert quote["status"] == "released"


async def test_audit_log_is_append_only(checkout: httpx.AsyncClient) -> None:
    async with checkout.app.state.session_factory() as session:  # type: ignore[attr-defined]
        with pytest.raises(Exception, match="append-only"):
            async with session.begin():
                await session.execute(text("UPDATE audit_log SET action = 'tampered'"))


def test_live_keys_are_refused_outside_production() -> None:
    with pytest.raises(ValueError, match="Live Stripe keys"):
        CheckoutSettings(
            database_url="postgresql+asyncpg://x/y",
            commerce_service_url="http://c",
            service_api_key="k",
            gateway_svc_api_key_hash="a" * 64,
            agent_svc_api_key_hash="b" * 64,
            stripe_secret_key="sk_live_" + "x" * 24,
            stripe_webhook_secret="whsec_" + "x" * 24,
        )
