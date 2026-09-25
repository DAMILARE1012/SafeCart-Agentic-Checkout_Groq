"""Service-to-service HTTP helpers."""

from __future__ import annotations

import re

import httpx
import structlog

from commerce_common.errors import NotFound

# Every id we mint is a short token (ord_…, quote_…, web_…, tg_-100…). Nothing else belongs in a path.
ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"  # also enforced on request models at the edges
PRINTABLE_PATTERN = r"^[^\x00-\x1f\x7f]*$"  # free text: no control characters (Postgres rejects NUL)
_ID = re.compile(ID_PATTERN)


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


def segment(value: str) -> str:
    """An id that is safe to place in a URL path of a service-to-service call.

    Ids arrive from requests; pasted into ``f"/v1/quotes/{id}"`` unchecked, ``../carts/x`` would steer
    the call to another endpoint and control characters would crash the client (found by API fuzzing).
    Anything that is not a well-formed id cannot exist, so it is a 404, never a 500.
    """
    if not _ID.fullmatch(value):
        raise NotFound("not_found", "No such resource")
    return value
