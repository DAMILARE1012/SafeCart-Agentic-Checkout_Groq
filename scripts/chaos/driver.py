"""Chaos driver: runs INSIDE the checkout-api container (it needs the internal network and Stripe key).

Invoked by scripts/chaos.py as ``python - < driver.py`` with the command in $CHAOS_CMD (JSON). Prints one
``CHAOS_RESULT {...}`` line. Payments are REAL Stripe test-mode PaymentIntents (so reconciliation can
verify them); the "payment succeeded" webhook is signed with the endpoint secret, exactly as Stripe signs.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid
from typing import Any

import httpx
import stripe
from sqlalchemy import text

from checkout_svc.audit import verify_chain
from checkout_svc.settings import CheckoutSettings
from commerce_common.db import create_engine, create_session_factory

S = CheckoutSettings()
AGENT = {"Authorization": f"Bearer {os.environ['CHAOS_AGENT_KEY']}"}
GATEWAY = {"Authorization": f"Bearer {os.environ['CHAOS_GATEWAY_KEY']}"}
CHECKOUT = "http://localhost:8000"
ADDRESS = {"line1": "1 Main St", "city": "Austin", "region": "TX", "postal_code": "78701", "country": "US"}


def key() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}


async def prepare(sku: str) -> dict[str, Any]:
    """Cart → quote → grant → confirm, exactly as the agent and the widget would."""
    conversation = f"chaos_{uuid.uuid4().hex[:16]}"
    async with httpx.AsyncClient(base_url=S.commerce_service_url, headers=AGENT, timeout=20) as commerce:
        cart = (await commerce.post("/v1/carts", json={"conversation_id": conversation})).json()
        added = await commerce.post(
            f"/v1/carts/{cart['id']}/items", json={"sku_id": sku, "quantity": 1}, headers=key()
        )
        added.raise_for_status()
        (await commerce.put(f"/v1/carts/{cart['id']}/shipping-address", json=ADDRESS)).raise_for_status()
        quote = (await commerce.post(f"/v1/carts/{cart['id']}/quotes", headers=key())).json()
    async with httpx.AsyncClient(base_url=CHECKOUT, headers=GATEWAY, timeout=30) as checkout:
        grant = await checkout.post(
            "/internal/confirmations", json={"quote_id": quote["id"], "conversation_id": conversation}
        )
        grant.raise_for_status()
        confirmed = await checkout.post(
            "/v1/checkout/confirm",
            json={"confirmation_token": grant.json()["token"], "conversation_id": conversation},
            headers=key(),
        )
        confirmed.raise_for_status()
    return {"order_id": confirmed.json()["order_id"]}


async def pay(order_id: str, deliveries: int, then_expire: bool) -> dict[str, Any]:
    """A real test-mode payment for the order's exact total, then the signed webhook (maybe redelivered)."""
    order = await one(
        "SELECT total_minor, currency, stripe_checkout_session_id FROM orders WHERE id = :o", o=order_id
    )
    client = stripe.StripeClient(S.stripe_secret_key.get_secret_value(), stripe_version=S.stripe_api_version)
    intent = client.v1.payment_intents.create(
        {
            "amount": order["total_minor"],
            "currency": order["currency"].lower(),
            "payment_method": "pm_card_visa",
            "payment_method_types": ["card"],
            "confirm": True,
            "metadata": {"order_id": order_id, "purpose": "chaos-test"},
        }
    )
    session = {
        "id": order["stripe_checkout_session_id"],
        "object": "checkout.session",
        "client_reference_id": order_id,
        "metadata": {"order_id": order_id},
        "payment_status": "paid",
        "amount_total": order["total_minor"],
        "currency": order["currency"].lower(),
        "payment_intent": intent.id,
        "url": None,
    }
    paid = {
        "id": f"evt_chaos_{uuid.uuid4().hex[:20]}",
        "type": "checkout.session.completed",
        "data": {"object": session},
    }
    statuses = [
        await deliver(paid) for _ in range(deliveries)
    ]  # same event id: Stripe's at-least-once redelivery
    if then_expire:  # a late, out-of-order event must not undo the payment
        expired = {
            "id": f"evt_chaos_{uuid.uuid4().hex[:20]}",
            "type": "checkout.session.expired",
            "data": {"object": {**session, "payment_status": "unpaid"}},
        }
        statuses.append(await deliver(expired))
    return {"payment_intent": intent.id, "webhook_statuses": statuses}


async def deliver(event: dict[str, Any]) -> int:
    payload = json.dumps(event).encode()
    ts = int(time.time())
    secret = S.stripe_webhook_secret.get_secret_value()
    signature = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(base_url=CHECKOUT, timeout=15) as checkout:
        response = await checkout.post(
            "/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": f"t={ts},v1={signature}", "Content-Type": "application/json"},
        )
    return response.status_code


async def one(sql: str, **params: Any) -> dict[str, Any]:
    engine = create_engine(S.database_url, pool_size=1, max_overflow=0)
    async with create_session_factory(engine)() as session:
        row = (await session.execute(text(sql), params)).mappings().one()
    await engine.dispose()
    return dict(row)


async def facts(order_ids: list[str]) -> dict[str, Any]:
    engine = create_engine(S.database_url, pool_size=1, max_overflow=0)
    out: dict[str, Any] = {"orders": {}}
    async with create_session_factory(engine)() as session:
        for order_id in order_ids:
            row = (
                (
                    await session.execute(
                        text("SELECT status, settlement, fulfillment_reference FROM orders WHERE id = :o"),
                        {"o": order_id},
                    )
                )
                .mappings()
                .one()
            )
            paid_events = await session.scalar(
                text(
                    "SELECT count(*) FROM outbox WHERE event_type = 'order.paid.v1' "
                    "AND envelope->'data'->>'order_id' = :o"
                ),
                {"o": order_id},
            )
            actions = list(
                (
                    await session.execute(
                        text("SELECT action FROM audit_log WHERE entity_id = :o ORDER BY id"), {"o": order_id}
                    )
                ).scalars()
            )
            out["orders"][order_id] = {**dict(row), "order_paid_events": paid_events, "audit": actions}
        out["unpublished_outbox"] = await session.scalar(
            text("SELECT count(*) FROM outbox WHERE published_at IS NULL")
        )
        out["audit_chain_ok"], _ = await verify_chain(session)
    await engine.dispose()
    return out


async def main() -> None:
    command = json.loads(os.environ["CHAOS_CMD"])
    if command["cmd"] == "prepare":
        result = await prepare(command["sku"])
    elif command["cmd"] == "pay":
        result = await pay(
            command["order_id"], command.get("deliveries", 1), command.get("then_expire", False)
        )
    elif command["cmd"] == "status":
        result = await one("SELECT status, settlement FROM orders WHERE id = :o", o=command["order_id"])
    else:
        result = await facts(command["order_ids"])
    print("CHAOS_RESULT " + json.dumps(result, default=str))


asyncio.run(main())
