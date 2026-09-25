"""Read-only view of orders in checkout-svc. The agent's key has ONLY `checkout:orders:read`:
it can tell the customer where an order is, never confirm, pay or refund."""

from __future__ import annotations

from typing import Any

import httpx

from commerce_common.http import service_client


class CheckoutReader:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    @classmethod
    def create(cls, base_url: str, api_key: str, *, timeout_s: float) -> CheckoutReader:
        return cls(service_client(base_url, api_key, httpx.Timeout(timeout_s)))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def latest_order(self, conversation_id: str) -> dict[str, Any] | None:
        response = await self._http.get(f"/v1/conversations/{conversation_id}/orders")
        response.raise_for_status()
        orders: list[dict[str, Any]] = response.json().get("orders", [])
        return orders[0] if orders else None
