"""Shared test kit for gateway-svc (helpers + fixtures).

Gateway tests: a fake agent-svc (httpx MockTransport) and a recording Telegram bot."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from commerce_common.sse import sse_event
from gateway_svc.agent_client import AgentClient
from gateway_svc.checkout_client import CheckoutClient
from gateway_svc.main import create_app
from gateway_svc.settings import GatewaySettings

ORIGIN = "https://shop.example"
PUBLISHABLE_KEY = "pk_widget_test"

PRODUCT = {
    "id": "prod_trail_gtx",
    "sku_id": "sku_trail_gtx_10_blk",
    "name": "Trail Runner GTX",
    "variant_label": "Men's 10 · Black",
    "description": "Waterproof.",
    "image_url": "https://img.example/tr.png",
    "price": {"amount_minor": 12900, "currency": "USD"},
    "compare_at_price": {"amount_minor": 14900, "currency": "USD"},
    "in_stock": True,
    "rating": 4.7,
}
QUOTE = {
    "id": "quote_1",
    "lines": [
        {"name": "Trail Runner GTX", "quantity": 1, "line_total": {"amount_minor": 12900, "currency": "USD"}}
    ],
    "subtotal": {"amount_minor": 12900, "currency": "USD"},
    "discounts": [],
    "shipping": {"amount_minor": 0, "currency": "USD"},
    "tax_total": {"amount_minor": 1064, "currency": "USD"},
    "tax_inclusive": False,
    "total": {"amount_minor": 13964, "currency": "USD"},
    "expires_at": "2030-01-01T00:00:00Z",
}


def turn_stream(
    text: str, blocks: list[dict[str, Any]] | None = None, suggestions: list[str] | None = None
) -> bytes:
    body = sse_event("turn.started", {"turn_id": "t1", "message_id": "msg_1"})
    for word in text.split(" "):
        body += sse_event("text.delta", {"message_id": "msg_1", "delta": word + " "})
    for block in blocks or []:
        body += sse_event("block", {"message_id": "msg_1", "block": block})
    if suggestions:
        body += sse_event("suggestions", {"message_id": "msg_1", "suggestions": suggestions})
    return body + sse_event("turn.completed", {"message_id": "msg_1"})


class FakeAgent:
    """Records what the gateway sends to agent-svc and answers with canned SSE."""

    def __init__(self) -> None:
        self.turns: list[tuple[str, dict[str, Any]]] = []
        self.reply: Callable[[dict[str, Any]], httpx.Response] = lambda _: httpx.Response(
            200, content=turn_stream("Hello there!"), headers={"content-type": "text/event-stream"}
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer gateway-key"
        path = request.url.path
        if path.endswith("/turns"):
            payload = json.loads(request.content)
            self.turns.append((path.split("/")[3], payload))
            return self.reply(payload)
        if path.endswith("/messages"):
            return httpx.Response(
                200, json={"messages": [{"id": "m1", "role": "user", "text": "hi", "blocks": []}]}
            )
        return httpx.Response(404)


class FakeCheckout:
    """Records gateway -> checkout-svc calls."""

    def __init__(self) -> None:
        self.grants: list[dict[str, Any]] = []
        self.confirms: list[tuple[dict[str, Any], str]] = []
        self.quote_payable = True

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer gateway-key"
        path = request.url.path
        if path == "/internal/confirmations":
            body = json.loads(request.content)
            self.grants.append(body)
            if not self.quote_payable:
                return httpx.Response(
                    409, json={"code": "quote_invalid", "message": "no", "retryable": False}
                )
            return httpx.Response(
                200, json={"token": "grant-" + "t" * 30, "expires_at": "2030-01-01T00:10:00Z"}
            )
        if path == "/v1/checkout/confirm":
            self.confirms.append((json.loads(request.content), request.headers["idempotency-key"]))
            return httpx.Response(
                200, json={"order_id": "ord_1", "checkout_url": "https://checkout.stripe.test/x"}
            )
        if path.startswith("/v1/orders/"):
            owner = request.url.params.get("conversation_id")
            return httpx.Response(200, json={"id": "ord_1", "status": "PAID", "owner": owner})
        return httpx.Response(404)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        self.sent.append(("message", {"chat_id": chat_id, "text": text, **kwargs}))

    async def send_photo(self, chat_id: int, photo: str, **kwargs: Any) -> None:
        self.sent.append(("photo", {"chat_id": chat_id, "photo": photo, **kwargs}))

    async def send_chat_action(self, chat_id: int, action: str, **kwargs: Any) -> None:
        self.sent.append(("action", {"chat_id": chat_id, "action": action}))

    async def answer_callback_query(self, callback_query_id: str, **kwargs: Any) -> None:
        self.sent.append(("answer", {"id": callback_query_id}))


def settings(**overrides: Any) -> GatewaySettings:
    base: dict[str, Any] = {
        "app_env": "test",
        "log_level": "WARNING",
        "log_format": "console",
        "agent_service_url": "http://agent",
        "checkout_service_url": "http://checkout",
        "service_api_key": "gateway-key",
        "jwt_secret": "x" * 40,
        "widget_publishable_key": PUBLISHABLE_KEY,
        "widget_allowed_origins": [ORIGIN],
        "cors_allowed_origins": [ORIGIN],
        "supported_currencies": ["USD", "EUR"],
        "rate_limit_messages_per_minute": 5,
        "telegram_enabled": True,
        "telegram_bot_token": "123:abc",
        "telegram_update_mode": "webhook",
        "telegram_webhook_url": "https://gw.example/telegram/webhook",
        "telegram_webhook_secret": "tg-secret",
    }
    return GatewaySettings(**{**base, **overrides})


@pytest.fixture
def fake_agent() -> FakeAgent:
    return FakeAgent()


@pytest.fixture
def fake_checkout() -> FakeCheckout:
    return FakeCheckout()


@pytest.fixture
def fake_bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def gateway(fake_agent: FakeAgent, fake_checkout: FakeCheckout, fake_bot: FakeBot) -> Callable[..., Any]:
    @asynccontextmanager
    async def factory(**overrides: Any) -> AsyncIterator[httpx.AsyncClient]:
        agent = AgentClient(
            httpx.AsyncClient(
                transport=httpx.MockTransport(fake_agent.handler),
                base_url="http://agent",
                headers={"Authorization": "Bearer gateway-key"},
            )
        )
        checkout = CheckoutClient(
            httpx.AsyncClient(
                transport=httpx.MockTransport(fake_checkout.handler),
                base_url="http://checkout",
                headers={"Authorization": "Bearer gateway-key"},
            )
        )
        app = create_app(settings(**overrides), agent=agent, checkout=checkout, telegram_bot=fake_bot)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client,
        ):
            client.app = app  # type: ignore[attr-defined]
            yield client

    return factory


async def new_session(client: httpx.AsyncClient, **body: Any) -> dict[str, Any]:
    response = await client.post(
        "/v1/sessions", json={"publishable_key": PUBLISHABLE_KEY, **body}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 200, response.text
    return response.json()
