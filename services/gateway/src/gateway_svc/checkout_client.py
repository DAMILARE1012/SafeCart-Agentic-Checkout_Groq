"""Client for checkout-svc. The gateway is the ONLY path by which a customer's click reaches payment."""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from commerce_common.http import service_client
from gateway_svc.agent_client import UpstreamError

log = structlog.get_logger("gateway.checkout")


class CheckoutClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    @classmethod
    def create(cls, base_url: str, api_key: str, *, timeout_s: float) -> CheckoutClient:
        # Confirm talks to commerce-svc and Stripe: allow a longer read than plain lookups.
        return cls(service_client(base_url, api_key, httpx.Timeout(timeout_s, read=30.0)))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise UpstreamError(
                503, {"code": "checkout_unavailable", "message": "Checkout is unavailable"}
            ) from exc
        body: dict[str, Any] = response.json() if response.content else {}
        if response.status_code >= 400:
            raise UpstreamError(response.status_code, body)
        return body

    async def issue_confirmation(self, quote_id: str, conversation_id: str) -> dict[str, Any] | None:
        """A single-use grant for the quote card, or None if it can't be paid (never fatal to a turn)."""
        try:
            return await self._call(
                "POST",
                "/internal/confirmations",
                json={"quote_id": quote_id, "conversation_id": conversation_id},
            )
        except UpstreamError as exc:
            log.warning("confirmation_not_issued", quote_id=quote_id, code=exc.code)
            return None

    async def confirm(self, token: str, conversation_id: str, idempotency_key: str) -> dict[str, Any]:
        return await self._call(
            "POST",
            "/v1/checkout/confirm",
            json={"confirmation_token": token, "conversation_id": conversation_id},
            headers={"Idempotency-Key": idempotency_key},
        )

    async def order(self, order_id: str, conversation_id: str) -> dict[str, Any]:
        return await self._call("GET", f"/v1/orders/{order_id}", params={"conversation_id": conversation_id})
