"""checkout-svc API process.  Run: ``uvicorn checkout_svc.main:create_app --factory``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from checkout_svc import api
from checkout_svc.commerce_client import CommerceClient
from checkout_svc.payments import PaymentProvider, StripePayments
from checkout_svc.service import CheckoutService
from checkout_svc.settings import CheckoutSettings
from commerce_common.app import create_service_app
from commerce_common.auth import CallerPolicy, ServiceAuth
from commerce_common.db import create_engine, create_session_factory, ping
from commerce_common.observability import configure_logging, setup_telemetry

VERSION = "0.1.0"


def create_app(
    settings: CheckoutSettings | None = None,
    *,
    payments: PaymentProvider | None = None,
    commerce: CommerceClient | None = None,
) -> FastAPI:
    """``payments``/``commerce`` are injectable for tests (fake Stripe, in-process commerce-svc)."""
    settings = settings or CheckoutSettings()  # values come from the environment
    configure_logging(settings)
    engine = create_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
    )
    sessions = create_session_factory(engine)
    commerce = commerce or CommerceClient.create(
        settings.commerce_service_url,
        settings.service_api_key.get_secret_value(),
        timeout_s=settings.internal_http_timeout_seconds,
        max_retries=settings.internal_http_max_retries,
    )
    payments = payments or StripePayments(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await commerce.aclose()
        await engine.dispose()

    app = create_service_app(
        title="checkout-svc",
        version=VERSION,
        settings=settings,
        lifespan=lifespan,
        readiness_checks={"database": lambda: ping(engine)},
    )
    app.state.settings = settings
    app.state.session_factory = sessions
    app.state.payments = payments
    app.state.commerce = commerce
    app.state.checkout = CheckoutService(sessions, settings, commerce, payments)
    app.state.service_auth = ServiceAuth(
        [
            CallerPolicy("gateway-svc", settings.gateway_svc_api_key_hash, api.CALLER_SCOPES["gateway-svc"]),
            CallerPolicy("agent-svc", settings.agent_svc_api_key_hash, api.CALLER_SCOPES["agent-svc"]),
        ]
    )
    app.include_router(api.internal)
    app.include_router(api.public)
    setup_telemetry(app, settings, engine)
    return app
