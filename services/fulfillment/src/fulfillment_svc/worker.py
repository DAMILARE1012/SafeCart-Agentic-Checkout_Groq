"""fulfillment-svc worker (compose: fulfillment-worker): RabbitMQ consumer + outbox relay + health.

Run: ``python -m fulfillment_svc.worker``.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time

import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from faststream import AckPolicy
from faststream.rabbit import RabbitBroker
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from commerce_common import events
from commerce_common.db import create_engine, create_session_factory
from commerce_common.messaging import consumer_queue, declare_dead_letter_queue, events_exchange, relay
from commerce_common.observability import configure_logging, setup_worker_telemetry, shutdown_telemetry
from fulfillment_svc.handler import handle_order_paid
from fulfillment_svc.models import OUTBOX
from fulfillment_svc.provider import FulfillmentProvider, build_provider
from fulfillment_svc.settings import FulfillmentSettings

log = structlog.get_logger("fulfillment.worker")

QUEUE = "fulfillment.order-paid"
TICK_S = 1.0
STALE_AFTER_S = 30.0


def build_broker(
    settings: FulfillmentSettings, sessions: async_sessionmaker[AsyncSession], provider: FulfillmentProvider
) -> RabbitBroker:
    broker = RabbitBroker(settings.rabbitmq_url, graceful_timeout=15)
    queue = consumer_queue(
        QUEUE,
        routing_key=events.ORDER_PAID,
        dead_letter_exchange=settings.events_dlx,
        max_deliveries=settings.event_max_delivery_attempts,
    )

    # NACK_ON_ERROR: a failure requeues the message; the quorum queue's delivery limit then dead-letters it.
    @broker.subscriber(queue, events_exchange(settings.events_exchange), ack_policy=AckPolicy.NACK_ON_ERROR)
    async def on_order_paid(message: events.Envelope) -> None:
        await handle_order_paid(sessions, provider, message, max_retries=settings.fulfillment_max_retries)

    return broker


async def run(settings: FulfillmentSettings) -> None:
    engine = create_engine(settings.database_url, pool_size=3, max_overflow=0)
    setup_worker_telemetry(settings, engine)
    sessions = create_session_factory(engine)
    broker = build_broker(settings, sessions, build_provider(settings))
    exchange = events_exchange(settings.events_exchange)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    last_success = time.monotonic()
    health = FastAPI()

    @health.get("/health")
    async def _health() -> JSONResponse:
        age = time.monotonic() - last_success
        ok = age < STALE_AFTER_S
        return JSONResponse(
            {"status": "ok" if ok else "stale", "last_success_s": round(age, 1)}, 200 if ok else 503
        )

    server = uvicorn.Server(
        uvicorn.Config(health, host="0.0.0.0", port=settings.worker_health_port, log_level="warning")  # noqa: S104
    )
    server_task = asyncio.create_task(server.serve())

    await broker.start()
    await broker.declare_exchange(exchange)
    await declare_dead_letter_queue(broker, settings.events_dlx)
    log.info("worker_started", queue=QUEUE)

    while not stop.is_set():
        try:
            await relay(sessions, OUTBOX, broker, exchange)
            last_success = time.monotonic()
        except Exception:
            log.exception("relay_failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=TICK_S)

    await broker.stop()
    server.should_exit = True
    await server_task
    await engine.dispose()
    shutdown_telemetry()
    log.info("worker_stopped")


def main() -> None:
    settings = FulfillmentSettings()
    configure_logging(settings)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
