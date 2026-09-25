"""Operator commands for checkout-svc (runbook actions, never run automatically).

    docker compose exec checkout-worker python -m checkout_svc.ops dead-webhooks          # list them
    docker compose exec checkout-worker python -m checkout_svc.ops retry-dead-webhooks    # after a fix

A Stripe event that failed every retry is parked (and alerted on by reconciliation). Once the cause is
fixed and deployed, ``retry-dead-webhooks`` gives those events a fresh set of attempts; the worker then
applies them through the normal, idempotent path. Every replay is written to the audit log.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select, update

from checkout_svc import audit
from checkout_svc.models import WebhookEvent
from checkout_svc.settings import CheckoutSettings
from checkout_svc.webhooks import MAX_ATTEMPTS
from commerce_common.db import create_engine, create_session_factory


async def main(command: str) -> int:
    settings = CheckoutSettings()
    engine = create_engine(settings.database_url, pool_size=1, max_overflow=0)
    sessions = create_session_factory(engine)
    dead = (WebhookEvent.processed_at.is_(None)) & (WebhookEvent.attempts >= MAX_ATTEMPTS)
    try:
        async with sessions() as session, session.begin():
            rows = (
                await session.execute(
                    select(WebhookEvent.event_id, WebhookEvent.type, WebhookEvent.last_error).where(dead)
                )
            ).all()
            for event_id, event_type, error in rows:
                print(f"{event_id}  {event_type}  {(error or '')[:100]}")
            if command == "retry-dead-webhooks" and rows:
                await session.execute(update(WebhookEvent).where(dead).values(attempts=0, last_error=None))
                for event_id, event_type, _ in rows:
                    await audit.record(
                        session,
                        actor_type="system",
                        actor_id="operator",
                        action="webhook.replayed",
                        entity_type="webhook",
                        entity_id=event_id,
                        after={"event_type": event_type},
                    )
                print(f"{len(rows)} dead webhook(s) queued for replay")
            elif not rows:
                print("no dead webhooks")
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"dead-webhooks", "retry-dead-webhooks"}:
        sys.exit("usage: python -m checkout_svc.ops {dead-webhooks|retry-dead-webhooks}")
    sys.exit(asyncio.run(main(sys.argv[1])))
