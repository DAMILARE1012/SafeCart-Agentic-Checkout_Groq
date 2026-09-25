"""commerce-svc API process.  Run: ``uvicorn commerce_svc.main:create_app --factory``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from commerce_common.app import create_service_app
from commerce_common.auth import CallerPolicy, ServiceAuth
from commerce_common.db import create_engine, create_session_factory, ping
from commerce_common.observability import configure_logging, setup_telemetry
from commerce_svc import api
from commerce_svc.crypto import AddressCipher
from commerce_svc.settings import CommerceSettings

VERSION = "0.1.0"


def create_app(settings: CommerceSettings | None = None) -> FastAPI:
    settings = settings or CommerceSettings()  # values come from the environment
    configure_logging(settings)
    engine = create_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    app = create_service_app(
        title="commerce-svc",
        version=VERSION,
        settings=settings,
        lifespan=lifespan,
        readiness_checks={"database": lambda: ping(engine)},
    )
    app.state.settings = settings
    app.state.session_factory = create_session_factory(engine)
    app.state.address_cipher = AddressCipher(settings.field_encryption_key.get_secret_value())
    app.state.service_auth = ServiceAuth(
        [
            CallerPolicy("agent-svc", settings.agent_svc_api_key_hash, api.CALLER_SCOPES["agent-svc"]),
            CallerPolicy(
                "checkout-svc", settings.checkout_svc_api_key_hash, api.CALLER_SCOPES["checkout-svc"]
            ),
        ]
    )
    for router in (api.catalog, api.carts, api.quotes, api.settlement):
        app.include_router(router)

    setup_telemetry(app, settings, engine)
    return app
