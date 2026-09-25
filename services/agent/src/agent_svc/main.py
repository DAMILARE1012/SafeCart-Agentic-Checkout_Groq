"""agent-svc API process.  Run: ``uvicorn agent_svc.main:create_app --factory``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from agent_svc import api
from agent_svc.checkout_reader import CheckoutReader
from agent_svc.commerce_client import CommerceClient
from agent_svc.graph import build_graph
from agent_svc.models import GroqModels, ModelProvider
from agent_svc.service import TurnService
from agent_svc.settings import AgentSettings
from agent_svc.turn_lock import InMemoryTurnLock, RedisTurnLock, TurnLock
from commerce_common.app import ReadinessCheck, create_service_app
from commerce_common.auth import CallerPolicy, ServiceAuth
from commerce_common.observability import configure_logging, setup_telemetry

VERSION = "0.1.0"


def create_app(
    settings: AgentSettings | None = None,
    *,
    models: ModelProvider | None = None,
    commerce: CommerceClient | None = None,
    checkout: CheckoutReader | None = None,
) -> FastAPI:
    """``models``/``commerce`` are injectable: tests use a scripted LLM and an in-process commerce-svc."""
    settings = settings or AgentSettings()  # values come from the environment
    configure_logging(settings)
    commerce = commerce or CommerceClient.create(
        settings.commerce_service_url,
        settings.service_api_key.get_secret_value(),
        timeout_s=settings.internal_http_timeout_seconds,
        max_retries=settings.internal_http_max_retries,
    )
    readiness: dict[str, ReadinessCheck] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            checkpointer: BaseCheckpointSaver[Any]
            if settings.langgraph_checkpointer == "postgres":
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
                from psycopg.rows import dict_row
                from psycopg_pool import AsyncConnectionPool

                pool = AsyncConnectionPool(
                    conninfo=settings.database_url,
                    max_size=settings.database_pool_size,
                    kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
                    open=False,
                )
                await pool.open()
                stack.push_async_callback(pool.close)
                checkpointer = AsyncPostgresSaver(pool)  # type: ignore[arg-type]

                async def db_ready() -> None:
                    async with pool.connection() as conn:
                        await conn.execute("SELECT 1")

                readiness["database"] = db_ready
            else:
                checkpointer = InMemorySaver()

            lock: TurnLock
            if settings.redis_url:
                from redis.asyncio import Redis

                redis = Redis.from_url(settings.redis_url)
                stack.push_async_callback(redis.aclose)
                lock = RedisTurnLock(redis)

                async def redis_ready() -> None:
                    await redis.ping()

                readiness["redis"] = redis_ready
            else:
                lock = InMemoryTurnLock()

            graph = build_graph(models or GroqModels(settings), settings, checkpointer)
            reader = checkout
            if reader is None and settings.checkout_service_url:
                reader = CheckoutReader.create(
                    settings.checkout_service_url,
                    settings.service_api_key.get_secret_value(),
                    timeout_s=settings.internal_http_timeout_seconds,
                )
                stack.push_async_callback(reader.aclose)
            app.state.turn_service = TurnService(graph, settings, commerce, reader)
            app.state.turn_lock = lock
            stack.push_async_callback(commerce.aclose)
            yield

    app = create_service_app(
        title="agent-svc", version=VERSION, settings=settings, lifespan=lifespan, readiness_checks=readiness
    )
    app.state.settings = settings
    app.state.service_auth = ServiceAuth(
        [CallerPolicy("gateway-svc", settings.gateway_svc_api_key_hash, api.CALLER_SCOPES["gateway-svc"])]
    )
    app.include_router(api.router)
    setup_telemetry(app, settings)
    return app
