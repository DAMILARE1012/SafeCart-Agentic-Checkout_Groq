"""Shared test kit for checkout-svc (helpers + fixtures).

Real Postgres (one container, separate commerce/checkout databases, like production), the real
commerce-svc in-process, and real Stripe webhook signature verification. Only Stripe's API call to
create a Checkout Session is faked.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer

from checkout_svc.commerce_client import CommerceClient
from checkout_svc.main import create_app as create_checkout_app
from checkout_svc.migrate import upgrade as upgrade_checkout
from checkout_svc.payments import (
    CheckoutSessionResult,
    verify_webhook,
)
from checkout_svc.settings import CheckoutSettings
from commerce_common.auth import hash_api_key
from commerce_common.db import create_engine, create_session_factory
from commerce_svc.main import create_app as create_commerce_app
from commerce_svc.migrate import upgrade as upgrade_commerce
from commerce_svc.seed import seed
from commerce_svc.settings import CommerceSettings

AGENT_KEY, GATEWAY_KEY, CHECKOUT_KEY = "agent-k", "gateway-k", "checkout-k"
WEBHOOK_SECRET = "whsec_test_" + "s" * 32
ADDRESS = {"line1": "1 Main St", "city": "Austin", "region": "TX", "postal_code": "78701", "country": "US"}


@dataclass
class FakeStripe:
    """Stands in for Stripe's Checkout Session API; webhook verification is Stripe's real code."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    fail_with: type[Exception] | None = None

    async def create_checkout_session(
        self, *, order_id: str, conversation_id: str, quote: dict[str, Any], idempotency_key: str
    ) -> CheckoutSessionResult:
        self.calls.append({"order_id": order_id, "quote": quote, "idempotency_key": idempotency_key})
        if self.fail_with:
            raise self.fail_with("stripe says no")
        return CheckoutSessionResult(
            session_id=f"cs_test_{order_id}",
            url=f"https://checkout.stripe.test/c/pay/{order_id}",
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )

    def parse_webhook(self, payload: bytes, signature: str | None) -> dict[str, Any]:
        return verify_webhook(payload, signature, WEBHOOK_SECRET, 300)


def sign(payload: bytes, secret: str = WEBHOOK_SECRET, timestamp: int | None = None) -> str:
    """Stripe-Signature header, computed exactly like Stripe does."""
    ts = timestamp or int(time.time())
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def session_event(kind: str, order: dict[str, Any], *, amount: int | None = None, paid: bool = True) -> bytes:
    event = {
        "id": f"evt_{uuid.uuid4().hex[:16]}",
        "type": kind,
        "data": {
            "object": {
                "id": f"cs_test_{order['id']}",
                "object": "checkout.session",
                "client_reference_id": order["id"],
                "metadata": {"order_id": order["id"]},
                "payment_status": "paid" if paid else "unpaid",
                "amount_total": order["total"]["amount_minor"] if amount is None else amount,
                "currency": order["total"]["currency"].lower(),
                "payment_intent": f"pi_test_{order['id']}",
                "url": None,
            }
        },
    }
    return json.dumps(event).encode()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresContainer]:
    with PostgresContainer("pgvector/pgvector:0.8.6-pg18", driver="asyncpg") as pg:
        yield pg


@pytest.fixture(scope="session")
async def databases(postgres: PostgresContainer) -> dict[str, str]:
    base = postgres.get_connection_url()
    admin = create_async_engine(base, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text("CREATE DATABASE checkout"))
    await admin.dispose()
    commerce_url, checkout_url = base, base.rsplit("/", 1)[0] + "/checkout"
    await asyncio.to_thread(upgrade_commerce, commerce_url)
    await asyncio.to_thread(upgrade_checkout, checkout_url)
    engine = create_engine(commerce_url, pool_size=1, max_overflow=0)
    async with create_session_factory(engine)() as session, session.begin():
        await seed(session)
    await engine.dispose()
    return {"commerce": commerce_url, "checkout": checkout_url}


@pytest.fixture(scope="session")
async def commerce_http(databases: dict[str, str]) -> AsyncIterator[httpx.AsyncClient]:
    app = create_commerce_app(
        CommerceSettings(
            database_url=databases["commerce"],
            app_env="test",
            log_level="WARNING",
            log_format="console",
            supported_currencies=["USD", "EUR"],
            field_encryption_key=Fernet.generate_key().decode(),
            agent_svc_api_key_hash=hash_api_key(AGENT_KEY),
            checkout_svc_api_key_hash=hash_api_key(CHECKOUT_KEY),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://commerce"
    ) as client:
        yield client


@pytest.fixture
def fake_stripe() -> FakeStripe:
    return FakeStripe()


@pytest.fixture
async def checkout(
    databases: dict[str, str], commerce_http: httpx.AsyncClient, fake_stripe: FakeStripe
) -> AsyncIterator[httpx.AsyncClient]:
    settings = CheckoutSettings(
        database_url=databases["checkout"],
        app_env="test",
        log_level="WARNING",
        log_format="console",
        commerce_service_url="http://commerce",
        service_api_key=CHECKOUT_KEY,
        gateway_svc_api_key_hash=hash_api_key(GATEWAY_KEY),
        agent_svc_api_key_hash=hash_api_key(AGENT_KEY),
        stripe_secret_key="sk_test_" + "x" * 24,
        stripe_webhook_secret=WEBHOOK_SECRET,
    )
    commerce = CommerceClient(
        httpx.AsyncClient(
            transport=commerce_http._transport,  # same in-process commerce-svc
            base_url="http://commerce",
            headers={"Authorization": f"Bearer {CHECKOUT_KEY}"},
        )
    )
    app = create_checkout_app(settings, payments=fake_stripe, commerce=commerce)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://checkout") as client,
    ):
        client.app = app  # type: ignore[attr-defined]
        yield client


GATEWAY = {"Authorization": f"Bearer {GATEWAY_KEY}"}
AGENT = {"Authorization": f"Bearer {AGENT_KEY}"}


async def quoted_cart(
    commerce_http: httpx.AsyncClient, sku: str = "sku_merino_socks_m", qty: int = 1
) -> dict[str, Any]:
    """Does what agent-svc does: cart → item → address → quote. Returns conversation + quote."""
    agent = {"Authorization": f"Bearer {AGENT_KEY}"}
    conversation = f"web_{uuid.uuid4().hex}"
    cart = (
        await commerce_http.post("/v1/carts", json={"conversation_id": conversation}, headers=agent)
    ).json()
    r = await commerce_http.post(
        f"/v1/carts/{cart['id']}/items",
        json={"sku_id": sku, "quantity": qty},
        headers={**agent, "Idempotency-Key": uuid.uuid4().hex},
    )
    assert r.status_code == 200, r.text
    await commerce_http.put(f"/v1/carts/{cart['id']}/shipping-address", json=ADDRESS, headers=agent)
    quote = (
        await commerce_http.post(
            f"/v1/carts/{cart['id']}/quotes", headers={**agent, "Idempotency-Key": uuid.uuid4().hex}
        )
    ).json()
    return {"conversation_id": conversation, "cart": cart, "quote": quote}


async def grant(checkout: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
    r = await checkout.post(
        "/internal/confirmations",
        json={"quote_id": data["quote"]["id"], "conversation_id": data["conversation_id"]},
        headers=GATEWAY,
    )
    assert r.status_code == 200, r.text
    return r.json()


async def confirm(
    checkout: httpx.AsyncClient, token: str, conversation_id: str, key: str | None = None
) -> httpx.Response:
    return await checkout.post(
        "/v1/checkout/confirm",
        json={"confirmation_token": token, "conversation_id": conversation_id},
        headers={**GATEWAY, "Idempotency-Key": key or uuid.uuid4().hex},
    )


async def deliver(
    checkout: httpx.AsyncClient, payload: bytes, signature: str | None = None
) -> httpx.Response:
    return await checkout.post(
        "/webhooks/stripe",
        content=payload,
        headers={"Stripe-Signature": signature or sign(payload), "Content-Type": "application/json"},
    )
