"""Widget API (docs §17.4). Public, behind Traefik; every route except /v1/sessions needs a session JWT."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Path, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from commerce_common.errors import DomainError
from commerce_common.sse import SSE_HEADERS, sse_comment, sse_event
from gateway_svc import sessions
from gateway_svc.agent_client import AgentClient
from gateway_svc.checkout_client import CheckoutClient
from gateway_svc.limits import RateLimiter
from gateway_svc.settings import GatewaySettings

KEEPALIVE_S = 15.0
router = APIRouter(prefix="/v1", tags=["widget"])
ConversationId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_\-]{1,64}$")]


class RateLimited(DomainError):
    http_status = 429
    retryable = True


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class SessionRequest(BaseModel):
    publishable_key: str = Field(min_length=1, max_length=200)
    locale: str = Field(default="en-US", max_length=35)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    resume_conversation_id: str | None = Field(default=None, max_length=64)


class UserAction(BaseModel):
    """Structured UI actions the widget may send. Anything else is rejected at the edge."""

    type: Literal[
        "add_to_cart",
        "update_cart_item",
        "remove_cart_item",
        "view_product",
        "start_checkout",
        "refresh_quote",
    ]
    sku_id: str | None = Field(default=None, max_length=40)
    cart_item_id: str | None = Field(default=None, max_length=40)
    product_id: str | None = Field(default=None, max_length=40)
    quantity: int | None = Field(default=None, ge=0, le=999)


class ConfirmBody(BaseModel):
    confirmation_token: str = Field(min_length=20, max_length=200)


class TurnBody(BaseModel):
    client_message_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_\-:.]+$")
    text: str = Field(max_length=1000)
    action: UserAction | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _settings(request: Request) -> GatewaySettings:
    settings: GatewaySettings = request.app.state.settings
    return settings


def client_ip(request: Request) -> str:
    # Traefik sets X-Forwarded-For; uvicorn runs with --proxy-headers so request.client is the real IP.
    return request.client.host if request.client else "unknown"


async def enforce_limits(request: Request, conversation_id: str) -> None:
    settings = _settings(request)
    limiter: RateLimiter = request.app.state.rate_limiter
    if not await limiter.allow(f"ip:{client_ip(request)}", settings.rate_limit_ip_requests_per_minute, 60):
        raise RateLimited("rate_limited", "Too many requests. Please wait a moment.")
    if not await limiter.allow(f"conv:{conversation_id}", settings.rate_limit_messages_per_minute, 60):
        raise RateLimited("rate_limited", "You're sending messages too quickly. Please wait a moment.")


async def decorate_block(
    block: dict[str, Any], session: sessions.WidgetSession, request: Request
) -> dict[str, Any]:
    """Quote blocks get their single-use confirmation grant HERE, from checkout-svc, on the way to the
    customer, so agent-svc never holds one. No grant (quote no longer payable) → the widget explains."""
    if block.get("type") != "quote":
        return block
    checkout: CheckoutClient = request.app.state.checkout_client
    grant = await checkout.issue_confirmation(block["quote"]["id"], session.conversation_id)
    confirmation = {"token": grant["token"], "expires_at": grant["expires_at"]} if grant else None
    return {**block, "confirmation": confirmation}


def agent_payload(
    body: TurnBody, session: sessions.WidgetSession, settings: GatewaySettings
) -> dict[str, Any]:
    return {
        "client_message_id": body.client_message_id,
        "text": body.text,
        "action": body.action.model_dump(exclude_none=True) if body.action else None,
        "context": {
            "currency": session.currency,
            "locale": session.locale,
            "channel": "web",
            "merchant_name": settings.merchant_name,
        },
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.post("/sessions")
async def create_session(body: SessionRequest, request: Request) -> dict[str, Any]:
    settings = _settings(request)
    sessions.check_origin(request, settings)
    sessions.check_publishable_key(body.publishable_key, settings)
    limiter: RateLimiter = request.app.state.rate_limiter
    if not await limiter.allow(
        f"session-ip:{client_ip(request)}", settings.rate_limit_ip_requests_per_minute, 60
    ):
        raise RateLimited("rate_limited", "Too many requests. Please wait a moment.")

    currency = body.currency if body.currency in settings.supported_currencies else settings.default_currency
    resume = body.resume_conversation_id
    conversation_id = (
        resume if resume and sessions.WEB_CONVERSATION.fullmatch(resume) else sessions.new_conversation_id()
    )
    session = sessions.WidgetSession(conversation_id=conversation_id, currency=currency, locale=body.locale)
    token, expires = sessions.issue(session, settings)
    return {
        "session_token": token,
        "expires_at": expires.isoformat(),
        "conversation_id": conversation_id,
        "merchant": {
            "id": settings.default_merchant_id,
            "name": settings.merchant_name,
            "logo_url": settings.merchant_logo_url,
            "currency": currency,
        },
    }


@router.get("/conversations/{conversation_id}/messages")
async def conversation_history(conversation_id: ConversationId, request: Request) -> dict[str, Any]:
    session = sessions.session_from_request(request, conversation_id)
    agent: AgentClient = request.app.state.agent_client
    history = await agent.history(conversation_id)
    # After a page reload, the most recent order summary is still payable if its quote is valid.
    for message in reversed(history.get("messages", [])):
        quotes = [i for i, b in enumerate(message.get("blocks", [])) if b.get("type") == "quote"]
        if quotes:
            i = quotes[-1]
            message["blocks"][i] = await decorate_block(message["blocks"][i], session, request)
            break
    return history


@router.post("/conversations/{conversation_id}/turns")
async def run_turn(
    conversation_id: ConversationId,
    body: TurnBody,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> StreamingResponse:
    session = sessions.session_from_request(request, conversation_id)
    if idempotency_key and idempotency_key != body.client_message_id:
        raise DomainError("idempotency_key_mismatch", "Idempotency-Key must equal client_message_id")
    await enforce_limits(request, conversation_id)

    agent: AgentClient = request.app.state.agent_client
    payload = agent_payload(body, session, _settings(request))
    request_id = request.headers.get("x-request-id")

    # Open the upstream stream first: an agent 409/4xx becomes this response's status, not a broken stream.
    stack = contextlib.AsyncExitStack()
    try:
        upstream = await stack.enter_async_context(
            agent.open_turn(conversation_id, payload, request_id=request_id)
        )
    except BaseException:
        await stack.aclose()
        raise

    # Upstream is drained by a task into a queue: timing out on queue.get() for keep-alives is safe,
    # whereas cancelling the upstream iterator itself would tear the stream down.
    queue: asyncio.Queue[tuple[str, Any] | BaseException | None] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for item in upstream:
                await queue.put(item)
        except Exception as exc:
            await queue.put(exc)
        finally:
            await queue.put(None)

    async def relay() -> AsyncIterator[bytes]:
        task = asyncio.create_task(pump())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
                except TimeoutError:
                    yield sse_comment()
                    continue
                if item is None:
                    break
                if isinstance(item, BaseException):
                    yield sse_event(
                        "turn.error",
                        {
                            "code": "assistant_unavailable",
                            "message": "Connection to the assistant was lost.",
                            "retryable": True,
                        },
                    )
                    break
                name, data = item
                if name == "block":
                    data = {**data, "block": await decorate_block(data["block"], session, request)}
                yield sse_event(name, data)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await stack.aclose()

    return StreamingResponse(relay(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/conversations/{conversation_id}/events")
async def conversation_events(conversation_id: ConversationId, request: Request) -> StreamingResponse:
    """Server push (order updates, compensation notices). Order events arrive with M4 (RabbitMQ);
    until then this holds the connection open with heartbeats so the widget shows 'connected'."""
    sessions.session_from_request(request, conversation_id)

    async def heartbeat() -> AsyncIterator[bytes]:
        yield sse_comment("connected")
        while not await request.is_disconnected():
            await asyncio.sleep(KEEPALIVE_S)
            yield sse_comment()

    return StreamingResponse(heartbeat(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/checkout/confirm")
async def confirm_checkout(
    body: ConfirmBody,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    """The customer's explicit "Confirm & pay" click. Goes straight to checkout-svc, never via the agent."""
    session = sessions.session_from_request(request)
    if not idempotency_key or len(idempotency_key) > 200:
        raise DomainError("idempotency_key_required", "Confirm requires an Idempotency-Key header")
    settings = _settings(request)
    limiter: RateLimiter = request.app.state.rate_limiter
    if not await limiter.allow(
        f"checkout:{session.conversation_id}", settings.rate_limit_checkouts_per_hour, 3600
    ):
        raise RateLimited("rate_limited", "Too many checkout attempts. Please try again later.")
    checkout: CheckoutClient = request.app.state.checkout_client
    return await checkout.confirm(body.confirmation_token, session.conversation_id, idempotency_key)


@router.get("/orders/{order_id}")
async def get_order(order_id: Annotated[str, Path(max_length=40)], request: Request) -> dict[str, Any]:
    session = sessions.session_from_request(request)
    checkout: CheckoutClient = request.app.state.checkout_client
    return await checkout.order(order_id, session.conversation_id)  # scoped: other customers' orders 404


# ---------------------------------------------------------------------------
# Stripe return pages (Stripe Checkout opens in its own tab; the storefront tab keeps the chat)
# ---------------------------------------------------------------------------
pages = APIRouter(prefix="/checkout", tags=["pages"], include_in_schema=False)

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;font-family:system-ui,sans-serif;
background:#f6f7f9;color:#27272a}}main{{max-width:420px;padding:32px;background:#fff;border-radius:16px;
box-shadow:0 8px 30px rgba(0,0,0,.08);text-align:center}}h1{{font-size:20px;margin:0 0 8px}}
p{{color:#52525b;line-height:1.5;margin:0}}</style></head>
<body><main><h1>{title}</h1><p>{body}</p></main></body></html>"""


@pages.get("/success", response_class=HTMLResponse)
async def checkout_success() -> str:
    # Deliberately NOT "payment successful": only Stripe's signed webhook decides that (docs P4).
    return _PAGE.format(
        title="Thanks! We're confirming your payment",
        body="You can close this tab. Your order status updates automatically in the chat.",
    )


@pages.get("/cancel", response_class=HTMLResponse)
async def checkout_cancel() -> str:
    return _PAGE.format(
        title="Payment cancelled",
        body="Nothing was charged. Your order summary is still in the chat if you change your mind.",
    )
