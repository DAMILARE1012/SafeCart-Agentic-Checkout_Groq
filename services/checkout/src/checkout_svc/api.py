"""checkout-svc HTTP API.

Internal (service keys): confirmations (gateway), confirm (gateway, on the customer's click), order reads.
Public (behind Traefik): POST /webhooks/stripe, authenticated by Stripe's signature, not a service key.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Path, Query, Request
from pydantic import BaseModel, Field

from checkout_svc import webhooks
from checkout_svc.payments import InvalidWebhook
from checkout_svc.service import CheckoutService
from commerce_common.auth import require_scope
from commerce_common.errors import BadRequest
from commerce_common.http import ID_PATTERN
from commerce_common.idempotency import MAX_KEY_LENGTH
from commerce_common.metrics import counter, prime_labels

WEBHOOKS_REJECTED = prime_labels(
    counter("checkout.webhooks.rejected", "Webhook requests that failed signature checks")
)

CALLER_SCOPES = {
    "gateway-svc": frozenset({"checkout:confirmations:issue", "checkout:confirm", "checkout:orders:read"}),
    "agent-svc": frozenset({"checkout:orders:read"}),  # read-only: the agent can never confirm or pay
}
ConversationId = Annotated[str, Field(min_length=1, max_length=64, pattern=ID_PATTERN)]
ResourceId = Annotated[str, Path(min_length=1, max_length=64, pattern=ID_PATTERN)]


class IssueConfirmationRequest(BaseModel):
    quote_id: str = Field(min_length=1, max_length=40, pattern=ID_PATTERN)
    conversation_id: ConversationId


class ConfirmRequest(BaseModel):
    confirmation_token: str = Field(min_length=20, max_length=200)
    conversation_id: ConversationId


def _service(request: Request) -> CheckoutService:
    service: CheckoutService = request.app.state.checkout
    return service


internal = APIRouter(tags=["checkout"])


@internal.post(
    "/internal/confirmations", dependencies=[Depends(require_scope("checkout:confirmations:issue"))]
)
async def issue_confirmation(body: IssueConfirmationRequest, request: Request) -> dict[str, Any]:
    """Single-use grant for one valid quote of one conversation. Returned to the gateway, never the agent."""
    return await _service(request).issue_confirmation(body.quote_id, body.conversation_id)


@internal.post("/v1/checkout/confirm", dependencies=[Depends(require_scope("checkout:confirm"))])
async def confirm(
    body: ConfirmRequest,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    """The customer's explicit confirmation. Idempotent per key: a retried click returns the same order."""
    if not idempotency_key or len(idempotency_key) > MAX_KEY_LENGTH:
        raise BadRequest("idempotency_key_required", "Confirm requires an Idempotency-Key header")
    return await _service(request).confirm(body.confirmation_token, body.conversation_id, idempotency_key)


@internal.get("/v1/orders/{order_id}", dependencies=[Depends(require_scope("checkout:orders:read"))])
async def get_order(
    order_id: ResourceId,
    request: Request,
    conversation_id: Annotated[str | None, Query(max_length=64, pattern=ID_PATTERN)] = None,
) -> dict[str, Any]:
    return await _service(request).get_order(order_id, conversation_id)


@internal.get(
    "/v1/conversations/{conversation_id}/orders",
    dependencies=[Depends(require_scope("checkout:orders:read"))],
)
async def conversation_orders(conversation_id: ResourceId, request: Request) -> dict[str, Any]:
    return {"orders": await _service(request).conversation_orders(conversation_id)}


public = APIRouter(tags=["webhooks"])


@public.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request, stripe_signature: Annotated[str | None, Header(alias="Stripe-Signature")] = None
) -> dict[str, bool]:
    """Verify → store once → 200 fast. Processing happens in the worker (Stripe retries non-2xx)."""
    try:
        stored = await webhooks.receive(
            request.app.state.session_factory,
            request.app.state.payments,
            await request.body(),
            stripe_signature,
        )
    except InvalidWebhook as exc:
        WEBHOOKS_REJECTED.add(1)  # forged, replayed or misconfigured secret: worth an alert if it spikes
        raise BadRequest("invalid_signature", "Webhook signature verification failed") from exc
    return {"received": True, "duplicate": not stored}
