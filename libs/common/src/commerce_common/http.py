"""Service-to-service HTTP helpers."""

from __future__ import annotations

import httpx
import structlog


async def propagate_request_id(request: httpx.Request) -> None:
    """httpx request hook: forward the current request id so one id follows a request across services
    (gateway → agent → commerce), joining their logs (docs P7)."""
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    if request_id and "x-request-id" not in request.headers:
        request.headers["X-Request-Id"] = str(request_id)


def service_client(base_url: str, api_key: str, timeout: httpx.Timeout) -> httpx.AsyncClient:
    """Pooled keep-alive client with service auth and request-id propagation."""
    return httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        event_hooks={"request": [propagate_request_id]},
    )
