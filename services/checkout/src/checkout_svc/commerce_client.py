"""commerce-svc client for checkout: read quotes/carts, lock a quote, commit or release an order."""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from commerce_common.http import service_client


class CommerceRejected(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class CommerceUnavailable(Exception):
    pass


def _retryable(exc: BaseException) -> bool:
    return isinstance(exc, httpx.TransportError) or (
        isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500
    )


class CommerceClient:
    """Every call here is idempotent (reads, or lock/commit/release keyed by order), so all are retried."""

    def __init__(self, http: httpx.AsyncClient, *, max_retries: int = 2) -> None:
        self._http = http
        self._attempts = max_retries + 1

    @classmethod
    def create(cls, base_url: str, api_key: str, *, timeout_s: float, max_retries: int) -> CommerceClient:
        return cls(service_client(base_url, api_key, httpx.Timeout(timeout_s)), max_retries=max_retries)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, json: Any = None) -> dict[str, Any]:
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._attempts),
                wait=wait_exponential_jitter(initial=0.2, max=2),
                retry=retry_if_exception(_retryable),
                reraise=True,
            ):
                with attempt:
                    response = await self._http.request(method, path, json=json)
                    if response.status_code >= 500:
                        response.raise_for_status()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            raise CommerceUnavailable(type(exc).__name__) from exc
        body: dict[str, Any] = response.json() if response.content else {}
        if response.status_code >= 400:
            raise CommerceRejected(
                response.status_code, str(body.get("code", "rejected")), str(body.get("message", "Rejected"))
            )
        return body

    async def get_quote(self, quote_id: str) -> dict[str, Any]:
        return await self._call("GET", f"/v1/quotes/{quote_id}")

    async def get_cart(self, cart_id: str) -> dict[str, Any]:
        return await self._call("GET", f"/v1/carts/{cart_id}")

    async def lock_quote(self, quote_id: str, order_id: str) -> dict[str, Any]:
        return await self._call("POST", f"/internal/quotes/{quote_id}/lock", {"order_id": order_id})

    async def commit(self, order_id: str) -> dict[str, Any]:
        return await self._call("POST", f"/internal/orders/{order_id}/commit")

    async def release(self, order_id: str) -> dict[str, Any]:
        return await self._call("POST", f"/internal/orders/{order_id}/release")

    async def void(self, order_id: str) -> dict[str, Any]:
        return await self._call("POST", f"/internal/orders/{order_id}/void")
