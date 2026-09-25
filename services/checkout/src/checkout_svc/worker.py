"""checkout-svc worker (compose: checkout-worker).

Loops: apply Stripe webhook events (inbox) → settle orders with commerce-svc → request fulfilment
(order.paid.v1 via the outbox) → publish the outbox → cancel orders abandoned mid-confirmation.
Consumes fulfillment.*.v1 to move PAID orders to FULFILLED / FULFILLMENT_FAILED. All steps are
idempotent and safe on several replicas (row locks + SKIP LOCKED + inbox).
Run: ``python -m checkout_svc.worker``.
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

from checkout_svc import settlement, webhooks
from checkout_svc.commerce_client import CommerceClient
from checkout_svc.fulfillment_results import handle_fulfillment_result
from checkout_svc.models import OUTBOX
from checkout_svc.settings import CheckoutSettings
from commerce_common import events
from commerce_common.db import create_engine, create_session_factory
from commerce_common.messaging import consumer_queue, declare_dead_letter_queue, events_exchange, relay
from commerce_common.observability import configure_logging

log = structlog.get_logger("checkout.worker")

TICK_S = 1.0
STALE_SWEEP_EVERY_S = 30.0
STALE_AFTER_S = 30.0  # health fails if the loop stops making progress
RESULTS_QUEUE = "checkout.fulfillment-results"


def build_broker(settings: CheckoutSettings, sessions: async_sessionmaker[AsyncSession]) -> RabbitBroker:
    if not settings.rabbitmq_url:
        raise RuntimeError("RABBITMQ_URL is required for the checkout worker")
    broker = RabbitBroker(settings.rabbitmq_url, graceful_timeout=15)
    queue = consumer_queue(
        RESULTS_QUEUE,
        routing_key="fulfillment.*.v1",
        dead_letter_exchange=settings.events_dlx,
        max_deliveries=settings.event_max_delivery_attempts,
    )

    # NACK_ON_ERROR: a failure requeues the message; the quorum queue's delivery limit then dead-letters it.
    @broker.subscriber(queue, events_exchange(settings.events_exchange), ack_policy=AckPolicy.NACK_ON_ERROR)
    async def on_fulfillment_result(message: events.Envelope) -> None:
        await handle_fulfillment_result(sessions, message)

    return broker


async def run(settings: CheckoutSettings) -> None:
    engine = create_engine(settings.database_url, pool_size=3, max_overflow=0)
    sessions = create_session_factory(engine)
    commerce = CommerceClient.create(
        settings.commerce_service_url,
        settings.service_api_key.get_secret_value(),
        timeout_s=settings.internal_http_timeout_seconds,
        max_retries=settings.internal_http_max_retries,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    broker = build_broker(settings, sessions)
    exchange = events_exchange(settings.events_exchange)

    last_success = time.monotonic()
    last_stale_sweep = 0.0
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
    log.info("worker_started")

    while not stop.is_set():
        try:
            applied = await webhooks.process_pending(sessions)
            settled = await settlement.settle_pending(sessions, commerce)
            requested = await settlement.request_fulfillment(sessions, commerce)
            published = await relay(sessions, OUTBOX, broker, exchange)
            cancelled = 0
            if time.monotonic() - last_stale_sweep > STALE_SWEEP_EVERY_S:
                cancelled = await settlement.cancel_stale_created(
                    sessions, settings.order_created_stale_seconds
                )
                last_stale_sweep = time.monotonic()
            if applied or settled or cancelled or requested or published:
                log.info(
                    "tick",
                    webhooks_applied=applied,
                    orders_settled=settled,
                    fulfillments_requested=requested,
                    events_published=published,
                    stale_cancelled=cancelled,
                )
            last_success = time.monotonic()
        except Exception:
            log.exception("tick_failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=TICK_S)

    await broker.stop()
    server.should_exit = True
    await server_task
    await commerce.aclose()
    await engine.dispose()
    log.info("worker_stopped")


def main() -> None:
    settings = CheckoutSettings()
    configure_logging(settings)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
