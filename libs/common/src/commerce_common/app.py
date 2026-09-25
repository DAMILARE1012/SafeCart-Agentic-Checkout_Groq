"""FastAPI app factory: one consistent shape for every service.

Provides: error model, request-id + access logging (pure ASGI, streaming-safe),
liveness/readiness endpoints, and docs disabled in production.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any

import structlog
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .errors import install_error_handlers
from .settings import ServiceSettings

ReadinessCheck = Callable[[], Awaitable[None]]
READINESS_TIMEOUT_S = 2.0
_QUIET_PATHS = frozenset({"/health/live", "/health/ready"})

log = structlog.get_logger("access")


class RequestContextMiddleware:
    """Propagates/creates ``X-Request-Id``, binds it to every log line, logs one access line per request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        request_id = headers.get("x-request-id") or uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if scope["path"] not in _QUIET_PATHS:
                log.info(
                    "request",
                    method=scope["method"],
                    path=scope["path"],
                    status=status_code,
                    duration_ms=round((time.perf_counter() - start) * 1000, 1),
                )
            structlog.contextvars.unbind_contextvars("request_id")


def create_service_app(
    *,
    title: str,
    version: str,
    settings: ServiceSettings,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]],
    readiness_checks: Mapping[str, ReadinessCheck] | None = None,
) -> FastAPI:
    app = FastAPI(
        title=title,
        version=version,
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    # Keep a reference (not a copy): services may register checks in their lifespan (e.g. a DB pool).
    checks = readiness_checks if readiness_checks is not None else {}

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    async def ready() -> JSONResponse:
        # Readiness checks only THIS service's own dependencies (db, broker), never
        # other services, so one slow service can't cascade "unready" upstream.
        results: dict[str, Any] = {}
        for name, check in checks.items():
            try:
                await asyncio.wait_for(check(), timeout=READINESS_TIMEOUT_S)
                results[name] = "ok"
            except Exception as exc:
                results[name] = f"failed: {type(exc).__name__}"
        healthy = all(v == "ok" for v in results.values())
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={"status": "ok" if healthy else "unavailable", "checks": results},
        )

    return app
