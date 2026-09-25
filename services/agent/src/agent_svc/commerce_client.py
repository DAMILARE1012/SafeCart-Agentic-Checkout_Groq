"""Typed HTTP client for commerce-svc. The ONLY way the agent touches carts, prices or quotes."""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from commerce_common.http import segment, service_client


class CommerceError(Exception):
    """Business rule rejection (4xx). Returned to the LLM so it can explain or correct."""

    def __init__(self, status: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class CommerceUnavailable(Exception):
    """Transport failure or 5xx after retries. Fails the turn (the cart is safe in commerce-svc)."""


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


class CommerceClient:
    def __init__(self, http: httpx.AsyncClient, *, max_retries: int = 2) -> None:
        self._http = http
        self._attempts = max_retries + 1

    @classmethod
    def create(cls, base_url: str, api_key: str, *, timeout_s: float, max_retries: int) -> CommerceClient:
        http = service_client(base_url, api_key, httpx.Timeout(timeout_s))
        return cls(http, max_retries=max_retries)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        # Retry only when repeating is safe: reads, idempotent verbs, or POSTs carrying an Idempotency-Key.
        safe = method in {"GET", "PUT", "PATCH", "DELETE"} or idempotency_key is not None
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._attempts if safe else 1),
                wait=wait_exponential_jitter(initial=0.2, max=2),
                retry=retry_if_exception(_retryable),
                reraise=True,
            ):
                with attempt:
                    response = await self._http.request(
                        method, path, json=json, params=params, headers=headers
                    )
                    if response.status_code >= 500:
                        response.raise_for_status()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            raise CommerceUnavailable(f"commerce-svc unavailable: {type(exc).__name__}") from exc

        body: dict[str, Any] = response.json() if response.content else {}
        if response.status_code >= 400:
            raise CommerceError(
                response.status_code,
                str(body.get("code", "commerce_error")),
                str(body.get("message", "Request rejected")),
                body.get("details"),
            )
        return body

    # -- catalog -----------------------------------------------------------------
    async def search(
        self,
        query: str,
        *,
        currency: str,
        limit: int = 5,
        max_price_minor: int | None = None,
        in_stock_only: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "q": query,
            "currency": currency,
            "limit": limit,
            "in_stock_only": in_stock_only,
        }
        if max_price_minor is not None:
            params["max_price_minor"] = max_price_minor
        return await self._request("GET", "/v1/products/search", params=params)

    async def get_product(self, product_id: str, *, currency: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"/v1/products/{segment(product_id)}", params={"currency": currency}
        )

    async def promotions(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/promotions")

    # -- carts ---------------------------------------------------------------------
    async def get_or_create_cart(self, conversation_id: str, currency: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/v1/carts", json={"conversation_id": conversation_id, "currency": currency}
        )

    async def get_cart(self, cart_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/carts/{segment(cart_id)}")

    async def add_item(
        self, cart_id: str, sku_id: str, quantity: int, *, idempotency_key: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/v1/carts/{segment(cart_id)}/items",
            json={"sku_id": sku_id, "quantity": quantity},
            idempotency_key=idempotency_key,
        )

    async def set_quantity(self, cart_id: str, item_id: str, quantity: int) -> dict[str, Any]:
        return await self._request(
            "PATCH", f"/v1/carts/{segment(cart_id)}/items/{segment(item_id)}", json={"quantity": quantity}
        )

    async def remove_item(self, cart_id: str, item_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/v1/carts/{segment(cart_id)}/items/{segment(item_id)}")

    async def apply_promo(self, cart_id: str, code: str) -> dict[str, Any]:
        return await self._request("PUT", f"/v1/carts/{segment(cart_id)}/promotion", json={"code": code})

    async def remove_promo(self, cart_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/v1/carts/{segment(cart_id)}/promotion")

    async def set_shipping_address(self, cart_id: str, address: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PUT", f"/v1/carts/{segment(cart_id)}/shipping-address", json=address)

    # -- quotes --------------------------------------------------------------------
    async def create_quote(self, cart_id: str, *, idempotency_key: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/v1/carts/{segment(cart_id)}/quotes", idempotency_key=idempotency_key
        )

    async def get_quote(self, quote_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/quotes/{segment(quote_id)}")
