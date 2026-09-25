"""checkout-svc worker (compose: checkout-worker).

Loops: apply Stripe webhook events (inbox) → settle orders with commerce-svc → refund orders that
couldn't be fulfilled (compensation) → request fulfilment (order.paid.v1 via the outbox) → publish the
outbox → alert on orders needing review → cancel orders abandoned mid-confirmation; periodically
reconcile with Stripe. Consumes fulfillment.*.v1 to move PAID orders to FULFILLED / FULFILLMENT_FAILED.
All steps are idempotent and safe on several replicas (row locks + SKIP LOCKED + inbox + advisory lock).
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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from checkout_svc import compensation, settlement, webhooks
from checkout_svc.commerce_client import CommerceClient
from checkout_svc.fulfillment_results import handle_fulfillment_result
from checkout_svc.models import OUTBOX, WebhookEvent
from checkout_svc.payments import StripePayments
from checkout_svc.reconciliation import Reconciler, alert_orders_needing_review
from checkout_svc.settings import CheckoutSettings
from commerce_common import events
from commerce_common.alerts import Alerter
from commerce_common.db import create_engine, create_session_factory
from commerce_common.messaging import (
    consumer_queue,
    declare_dead_letter_queue,
    events_exchange,
    queue_depth,
    relay,
)
from commerce_common.metrics import gauge
from commerce_common.observability import configure_logging, setup_worker_telemetry, shutdown_telemetry

log = structlog.get_logger("checkout.worker")

TICK_S = 1.0
STALE_SWEEP_EVERY_S = 30.0
STALE_AFTER_S = 30.0  # health fails if the loop stops making progress
RESULTS_QUEUE = "checkout.fulfillment-results"
OUTBOX_BACKLOG = gauge("checkout.outbox.backlog", "Events staged but not yet published")
INBOX_BACKLOG = gauge("checkout.webhooks.backlog", "Stripe webhooks received but not yet applied")


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


async def record_backlogs(sessions: async_sessionmaker[AsyncSession]) -> None:
    """Two index-only counts per tick: the earliest signal that something downstream is stuck."""
    async with sessions() as session:
        OUTBOX_BACKLOG.set(
            await session.scalar(
                select(func.count()).select_from(OUTBOX).where(OUTBOX.c.published_at.is_(None))
            )
            or 0
        )
        INBOX_BACKLOG.set(
            await session.scalar(
                select(func.count())
                .select_from(WebhookEvent)
                # Only events still being retried: dead ones (every retry failed) have their own gauge.
                .where(WebhookEvent.processed_at.is_(None), WebhookEvent.attempts < webhooks.MAX_ATTEMPTS)
            )
            or 0
        )


async def run(settings: CheckoutSettings) -> None:
    engine = create_engine(settings.database_url, pool_size=5, max_overflow=0)
    setup_worker_telemetry(settings, engine)
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
    payments = StripePayments(settings)
    alert_url = settings.alert_webhook_url.get_secret_value() if settings.alert_webhook_url else None
    alerter = Alerter(alert_url, source="checkout-svc")
    rabbitmq_url = settings.rabbitmq_url or ""
    reconciler = Reconciler(
        sessions,
        payments,
        alerter,
        settings,
        dead_letter_depth=lambda: queue_depth(rabbitmq_url, f"{settings.events_dlx}.q"),
    )
    last_reconciliation = float("-inf")  # first run right after start: catches up anything missed while down

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
            refunded = await compensation.refund_failed_orders(
                sessions, payments, auto_refund=settings.compensation_auto_refund_enabled
            )
            requested = await settlement.request_fulfillment(sessions, commerce)
            published = await relay(sessions, OUTBOX, broker, exchange)
            alerted = await alert_orders_needing_review(sessions, alerter)
            await record_backlogs(sessions)
            if (
                settings.reconciliation_enabled
                and time.monotonic() - last_reconciliation > settings.reconciliation_interval_seconds
            ):
                await reconciler.run()
                last_reconciliation = time.monotonic()
            cancelled = 0
            if time.monotonic() - last_stale_sweep > STALE_SWEEP_EVERY_S:
                cancelled = await settlement.cancel_stale_created(
                    sessions, settings.order_created_stale_seconds
                )
                last_stale_sweep = time.monotonic()
            if applied or settled or cancelled or requested or published or refunded or alerted:
                log.info(
                    "tick",
                    webhooks_applied=applied,
                    orders_settled=settled,
                    refunds_issued=refunded,
                    review_alerts=alerted,
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
    await alerter.aclose()
    await engine.dispose()
    shutdown_telemetry()
    log.info("worker_stopped")


def main() -> None:
    settings = CheckoutSettings()
    configure_logging(settings)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
