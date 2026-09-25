"""gateway-svc process.  Run: ``uvicorn gateway_svc.main:create_app --factory``."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from commerce_common.app import ReadinessCheck, create_service_app
from commerce_common.errors import Forbidden, NotFound
from commerce_common.observability import configure_logging, setup_telemetry
from gateway_svc import web
from gateway_svc.agent_client import AgentClient
from gateway_svc.checkout_client import CheckoutClient
from gateway_svc.limits import Deduper, InMemoryLimits, RateLimiter, RedisDeduper, RedisRateLimiter
from gateway_svc.settings import GatewaySettings
from gateway_svc.telegram_channel import BotApi, TelegramChannel, poll_updates

VERSION = "0.1.0"
log = structlog.get_logger("gateway")


def create_app(
    settings: GatewaySettings | None = None,
    *,
    agent: AgentClient | None = None,
    checkout: CheckoutClient | None = None,
    telegram_bot: BotApi | None = None,
) -> FastAPI:
    """``agent``/``checkout``/``telegram_bot`` are injectable for tests."""
    settings = settings or GatewaySettings()  # values come from the environment
    configure_logging(settings)
    readiness: dict[str, ReadinessCheck] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            if settings.redis_url:
                from redis.asyncio import Redis

                redis = Redis.from_url(settings.redis_url)
                stack.push_async_callback(redis.aclose)
                rate_limiter: RateLimiter = RedisRateLimiter(redis)
                deduper: Deduper = RedisDeduper(redis)

                async def redis_ready() -> None:
                    await redis.ping()

                readiness["redis"] = redis_ready
            else:
                limits = InMemoryLimits()
                rate_limiter, deduper = limits, limits
            app.state.rate_limiter = rate_limiter

            client = agent or AgentClient.create(
                settings.agent_service_url,
                settings.service_api_key.get_secret_value(),
                timeout_s=settings.internal_http_timeout_seconds,
            )
            stack.push_async_callback(client.aclose)
            app.state.agent_client = client
            checkout_client = checkout or CheckoutClient.create(
                settings.checkout_service_url,
                settings.service_api_key.get_secret_value(),
                timeout_s=settings.internal_http_timeout_seconds,
            )
            stack.push_async_callback(checkout_client.aclose)
            app.state.checkout_client = checkout_client

            app.state.telegram = None
            if settings.telegram_enabled:
                bot: Any = telegram_bot
                if bot is None:
                    from telegram import Bot

                    assert settings.telegram_bot_token is not None
                    bot = Bot(
                        settings.telegram_bot_token.get_secret_value(),
                        base_url=f"{settings.telegram_api_base_url}/bot",
                    )
                    await bot.initialize()
                    stack.push_async_callback(bot.shutdown)
                channel = TelegramChannel(
                    bot, client, settings, deduper=deduper, limiter=app.state.rate_limiter
                )
                stack.push_async_callback(channel.drain)
                app.state.telegram = channel
                if telegram_bot is None:
                    await _start_telegram(settings, bot, channel, stack)
            yield

    app = create_service_app(
        title="gateway-svc", version=VERSION, settings=settings, lifespan=lifespan, readiness_checks=readiness
    )
    app.state.settings = settings
    # Bearer tokens, not cookies: no credentials mode, so CORS stays simple and CSRF doesn't apply.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-Id"],
        expose_headers=["X-Request-Id"],
        max_age=600,
    )
    app.include_router(web.router)
    app.include_router(web.pages)

    @app.post("/telegram/webhook", include_in_schema=False)
    async def telegram_webhook(request: Request) -> dict[str, bool]:
        channel: TelegramChannel | None = request.app.state.telegram
        if channel is None or settings.telegram_update_mode != "webhook":
            raise NotFound("not_found", "Not found")
        expected = (
            settings.telegram_webhook_secret.get_secret_value() if settings.telegram_webhook_secret else ""
        )
        presented = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not expected or not hmac.compare_digest(presented.encode(), expected.encode()):
            raise Forbidden("forbidden", "Invalid webhook secret")
        from telegram import Update

        channel.submit(Update.de_json(await request.json(), None))
        return {"ok": True}  # acknowledge fast; Telegram retries slow webhooks

    setup_telemetry(app, settings)
    return app


async def _start_telegram(
    settings: GatewaySettings, bot: Any, channel: TelegramChannel, stack: AsyncExitStack
) -> None:
    if settings.telegram_update_mode == "webhook":
        assert settings.telegram_webhook_url
        assert settings.telegram_webhook_secret
        await bot.set_webhook(
            url=settings.telegram_webhook_url,
            secret_token=settings.telegram_webhook_secret.get_secret_value(),
            allowed_updates=settings.telegram_allowed_updates,
        )
        log.info("telegram_webhook_registered")
        return
    task = asyncio.create_task(poll_updates(bot, channel, settings.telegram_allowed_updates))

    async def stop() -> None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    stack.push_async_callback(stop)
