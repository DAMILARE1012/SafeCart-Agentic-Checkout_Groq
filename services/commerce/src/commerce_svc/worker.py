"""commerce-svc worker process (compose: commerce-worker).

M1 jobs: expire stale quotes, purge old idempotency keys. The outbox relay
and event consumers (inventory commit/release, promo redemption) join in M4.
Every job is a single idempotent UPDATE/DELETE, so running it on several
replicas at once is safe.
Run: ``python -m commerce_svc.worker``.
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

from commerce_common.db import create_engine, create_session_factory
from commerce_common.idempotency import purge_expired
from commerce_common.metrics import counter
from commerce_common.observability import configure_logging, setup_worker_telemetry, shutdown_telemetry
from commerce_svc.models import IDEMPOTENCY_KEYS
from commerce_svc.quotes import expire_stale_quotes
from commerce_svc.reservations import release_expired_reservations
from commerce_svc.settings import CommerceSettings

log = structlog.get_logger("commerce.worker")
RESERVATIONS_EXPIRED = counter("commerce.reservations.expired", "Stock reservations released by TTL")

QUOTE_SWEEP_S = 30
PURGE_EVERY_S = 3600
STALE_AFTER_S = 5 * QUOTE_SWEEP_S  # health fails if the loop stops making progress


async def run(settings: CommerceSettings) -> None:
    engine = create_engine(settings.database_url, pool_size=2, max_overflow=0)
    setup_worker_telemetry(settings, engine)
    sessions = create_session_factory(engine)
    last_success = time.monotonic()
    last_purge = 0.0
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # Windows dev machines
            loop.add_signal_handler(sig, stop.set)

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

    log.info("worker_started")
    while not stop.is_set():
        try:
            async with sessions() as session, session.begin():
                expired = await expire_stale_quotes(session)
                released = await release_expired_reservations(session, settings)
                purged = 0
                if time.monotonic() - last_purge > PURGE_EVERY_S:
                    purged = await purge_expired(
                        session, IDEMPOTENCY_KEYS, older_than_hours=settings.idempotency_ttl_hours
                    )
                    last_purge = time.monotonic()
            RESERVATIONS_EXPIRED.add(released)
            if expired or purged or released:
                log.info(
                    "sweep",
                    quotes_expired=expired,
                    reservations_released=released,
                    idempotency_keys_purged=purged,
                )
            last_success = time.monotonic()
        except Exception:
            log.exception("sweep_failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=QUOTE_SWEEP_S)

    server.should_exit = True
    await server_task
    await engine.dispose()
    shutdown_telemetry()
    log.info("worker_stopped")


def main() -> None:
    settings = CommerceSettings()
    configure_logging(settings)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
