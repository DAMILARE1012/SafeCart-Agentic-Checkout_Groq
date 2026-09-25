"""Payment provider boundary. Stripe in deployments; a fake in tests (same webhook verification)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import stripe
from stripe import (
    APIConnectionError,
    APIError,
    IdempotencyError,
    RateLimitError,
    SignatureVerificationError,
    StripeError,
)

from checkout_svc.settings import CheckoutSettings


@dataclass(frozen=True, slots=True)
class CheckoutSessionResult:
    session_id: str
    url: str
    expires_at: datetime


class PaymentTemporarilyUnavailable(Exception):
    """Network/5xx/rate limit: safe to retry later with the SAME idempotency key."""


class PaymentRejected(Exception):
    """Stripe refused the request (bad params, auth): retrying won't help."""


class InvalidWebhook(Exception):
    pass


class PaymentProvider(Protocol):
    async def create_checkout_session(
        self,
        *,
        order_id: str,
        conversation_id: str,
        quote: dict[str, Any],
        idempotency_key: str,
    ) -> CheckoutSessionResult: ...

    def parse_webhook(self, payload: bytes, signature: str | None) -> dict[str, Any]: ...


def order_description(quote: dict[str, Any]) -> str:
    items = "; ".join(f"{line['quantity']}× {line['name']}" for line in quote["lines"])
    return items[:480] or "Order"


class StripePayments:
    def __init__(self, settings: CheckoutSettings) -> None:
        self._settings = settings
        self._client = stripe.StripeClient(
            settings.stripe_secret_key.get_secret_value(),
            stripe_version=settings.stripe_api_version,
            max_network_retries=settings.stripe_max_network_retries,  # SDK retries reuse the idempotency key
            http_client=stripe.HTTPXClient(),
        )

    async def create_checkout_session(
        self,
        *,
        order_id: str,
        conversation_id: str,
        quote: dict[str, Any],
        idempotency_key: str,
    ) -> CheckoutSessionResult:
        s = self._settings
        expires_at = datetime.now(UTC) + timedelta(minutes=s.checkout_session_expiry_minutes)
        metadata = {"order_id": order_id, "quote_id": quote["id"], "conversation_id": conversation_id}
        params: dict[str, Any] = {
            "mode": "payment",
            "client_reference_id": order_id,
            # ONE line for exactly the quoted total: items, discounts, shipping and tax were all computed by
            # commerce-svc. Stripe must not recompute anything, so the charge equals the quote to the cent.
            "line_items": [
                {
                    "quantity": 1,
                    "price_data": {
                        "currency": quote["total"]["currency"].lower(),
                        "unit_amount": int(quote["total"]["amount_minor"]),
                        "product_data": {
                            "name": f"{s.merchant_name} order {order_id[-8:].upper()}",
                            "description": order_description(quote),
                        },
                    },
                }
            ],
            "metadata": metadata,
            "payment_intent_data": {"metadata": metadata},
            "success_url": f"{s.public_base_url}{s.checkout_success_path}?order_id={order_id}",
            "cancel_url": f"{s.public_base_url}{s.checkout_cancel_path}?order_id={order_id}",
            "expires_at": int(expires_at.timestamp()),
        }
        try:
            session = await self._client.v1.checkout.sessions.create_async(
                params,  # type: ignore[arg-type]
                {"idempotency_key": idempotency_key},
            )
        except (APIConnectionError, RateLimitError, APIError) as exc:
            raise PaymentTemporarilyUnavailable(type(exc).__name__) from exc
        except IdempotencyError as exc:  # same key, different params: a bug, never retry blindly
            raise PaymentRejected(f"idempotency conflict: {exc.user_message or exc}") from exc
        except StripeError as exc:
            raise PaymentRejected(str(exc.user_message or exc)) from exc
        if not session.url:
            raise PaymentRejected("Stripe returned no checkout URL")
        return CheckoutSessionResult(session_id=session.id, url=session.url, expires_at=expires_at)

    def parse_webhook(self, payload: bytes, signature: str | None) -> dict[str, Any]:
        return verify_webhook(
            payload,
            signature,
            self._settings.stripe_webhook_secret.get_secret_value(),
            self._settings.stripe_webhook_tolerance_seconds,
        )


def verify_webhook(payload: bytes, signature: str | None, secret: str, tolerance: int) -> dict[str, Any]:
    """Stripe's own signature check (HMAC-SHA256 + timestamp tolerance against replays)."""
    if not signature:
        raise InvalidWebhook("missing Stripe-Signature header")
    try:
        stripe.WebhookSignature.verify_header(payload.decode("utf-8"), signature, secret, tolerance)
    except (SignatureVerificationError, ValueError) as exc:
        raise InvalidWebhook(str(exc)) from exc
    event: dict[str, Any] = json.loads(payload)
    return event
