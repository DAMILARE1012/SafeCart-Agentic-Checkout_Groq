"""Reconciliation and alerting (docs §7.5, §11): trust, but verify against Stripe.

Webhooks drive order state, but a webhook can be lost (endpoint down longer than Stripe retries,
a misconfigured secret, a bug). Every ``RECONCILIATION_INTERVAL_SECONDS`` one worker replica (Postgres
advisory lock) compares our orders with Stripe and:

* replays payments and refunds Stripe has but we missed, through ``webhooks.apply_event``, the same
  rules a real webhook would hit (amount checks included), attributed to ``system/reconciler``;
* verifies every paid order once against its PaymentIntent (amount and currency actually received);
  a mismatch is audited, a still-PAID order is stopped in MANUAL_REVIEW, and a human is alerted;
* alerts on orders stuck in a state (refund not starting, fulfilment not answering), webhook events
  that exhausted their retries, dead-lettered messages, and a broken audit hash chain.

Separately, every worker tick, ``alert_orders_needing_review`` tells a human about each order that
entered MANUAL_REVIEW, exactly once: the order's ``attention_alerted_at`` is set only after delivery.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from checkout_svc import audit, orders, webhooks
from checkout_svc.models import Order, WebhookEvent
from checkout_svc.payments import PaymentProvider, PaymentRejected, PaymentTemporarilyUnavailable
from checkout_svc.settings import CheckoutSettings
from commerce_common.alerts import Alerter
from commerce_common.metrics import counter, observed, prime_labels

log = structlog.get_logger("checkout.reconciliation")

ACTOR = "reconciler"
LOCK_KEY = 0x7265636F  # 'reco': one reconciler at a time across worker replicas
BATCH = 100
RECONCILED = prime_labels(
    counter("checkout.reconciliation.fixes", "Missed webhooks applied by reconciliation, by kind"),
    [{"kind": "payment"}, {"kind": "refund"}],
)
MISMATCHES = prime_labels(counter("checkout.reconciliation.mismatches", "Orders that disagree with Stripe"))
# Results of the latest run, reported on EVERY metrics export (the run itself is every 15 minutes).
LATEST: dict[str, float] = {}
observed(
    "checkout.orders.stuck", "Orders stuck in a state at the last reconciliation", lambda: LATEST.get("stuck")
)
observed("events.dead_letters", "Messages in the dead-letter queue", lambda: LATEST.get("dead_letters"))
observed(
    "checkout.webhooks.dead", "Stripe webhooks that failed every retry", lambda: LATEST.get("dead_webhooks")
)
observed(
    "checkout.audit.chain_ok",
    "1 if the audit hash chain verified at the last run",
    lambda: LATEST.get("chain_ok"),
    unit="{bool}",
)
# Orders whose Stripe payment is final: verified once against the PaymentIntent.
VERIFIABLE = ("PAID", "FULFILLED", "FULFILLMENT_FAILED", "REFUND_PENDING", "REFUNDED")


def _money(minor: int, currency: str) -> str:
    return f"{minor} {currency} (minor units)"


async def alert_orders_needing_review(sessions: async_sessionmaker[AsyncSession], alerter: Alerter) -> int:
    async with sessions() as session:
        rows = (
            await session.execute(
                select(Order.id, Order.failure_reason, Order.total_minor, Order.currency)
                .where(Order.status == "MANUAL_REVIEW", Order.attention_alerted_at.is_(None))
                .limit(50)
            )
        ).all()
    alerted = 0
    for order_id, reason, total_minor, currency in rows:
        delivered = await alerter.send(
            f"order:{order_id}:manual_review",
            "Order needs manual review",
            details={"order_id": order_id, "reason": reason, "total": _money(total_minor, currency)},
            dedup=False,  # the marker below is what makes this exactly-once
        )
        if not delivered:
            continue  # retried next tick
        async with sessions() as session, session.begin():
            await session.execute(
                update(Order).where(Order.id == order_id).values(attention_alerted_at=datetime.now(UTC))
            )
        alerted += 1
    return alerted


@dataclass
class ReconciliationReport:
    payments_recovered: int = 0
    refunds_recovered: int = 0
    orders_verified: int = 0
    mismatches: int = 0
    stuck_orders: int = 0
    dead_webhooks: int = 0
    dead_letters: int = 0
    audit_chain_ok: bool = True
    stripe_errors: int = 0


class Reconciler:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        payments: PaymentProvider,
        alerter: Alerter,
        settings: CheckoutSettings,
        *,
        dead_letter_depth: Callable[[], Awaitable[int]] | None = None,
    ) -> None:
        self._sessions = sessions
        self._payments = payments
        self._alerter = alerter
        self._settings = settings
        self._dead_letter_depth = dead_letter_depth

    async def run(self) -> ReconciliationReport | None:
        """None when another replica is already reconciling."""
        async with self._sessions() as lock_session:
            if not await lock_session.scalar(select(func.pg_try_advisory_lock(LOCK_KEY))):
                return None
            try:
                report = await self._run()
            finally:
                await lock_session.execute(select(func.pg_advisory_unlock(LOCK_KEY)))
                await lock_session.commit()
        RECONCILED.add(report.payments_recovered, {"kind": "payment"})
        RECONCILED.add(report.refunds_recovered, {"kind": "refund"})
        MISMATCHES.add(report.mismatches)
        LATEST.update(
            stuck=report.stuck_orders,
            dead_letters=report.dead_letters,
            dead_webhooks=report.dead_webhooks,
            chain_ok=1 if report.audit_chain_ok else 0,
        )
        log.info("reconciliation_completed", **report.__dict__)
        return report

    async def _run(self) -> ReconciliationReport:
        report = ReconciliationReport()
        now = datetime.now(UTC)
        since = now - timedelta(hours=self._settings.reconciliation_lookback_hours)
        settled_before = now - timedelta(minutes=self._settings.reconciliation_grace_minutes)

        await self._recover_payments(report, since, settled_before)
        await self._recover_refunds(report, settled_before)
        await self._verify_payments(report, since)
        await self._stuck_orders(report, now)
        await self._system_checks(report)
        return report

    # ------------------------------------------------------------------ missed webhooks
    async def _recover_payments(
        self, report: ReconciliationReport, since: datetime, before: datetime
    ) -> None:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(Order.id, Order.stripe_checkout_session_id)
                    .where(
                        Order.status == "AWAITING_PAYMENT",
                        Order.stripe_checkout_session_id.is_not(None),
                        Order.updated_at.between(since, before),
                    )
                    .limit(BATCH)
                )
            ).all()
        for order_id, session_id in rows:
            stripe_session = await self._fetch(report, self._payments.retrieve_checkout_session(session_id))
            if stripe_session is None:
                continue
            if stripe_session.get("status") == "complete" and stripe_session.get("payment_status") == "paid":
                event_type = "checkout.session.completed"
            elif stripe_session.get("status") == "expired":
                event_type = "checkout.session.expired"
            else:
                continue  # still open: the customer may yet pay
            if await self._replay(order_id, event_type, stripe_session):
                report.payments_recovered += 1
                await self._alerter.send(
                    "stripe:missed_webhooks",
                    "Stripe webhooks were missed; reconciliation applied them",
                    severity="warning",
                    details={"example_order_id": order_id, "event_type": event_type},
                )

    async def _recover_refunds(self, report: ReconciliationReport, before: datetime) -> None:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(Order.id, Order.stripe_refund_id)
                    .where(
                        Order.status == "REFUND_PENDING",
                        Order.stripe_refund_id.is_not(None),
                        Order.updated_at < before,
                    )
                    .limit(BATCH)
                )
            ).all()
        for order_id, refund_id in rows:
            refund = await self._fetch(report, self._payments.retrieve_refund(refund_id))
            if refund is not None and await self._replay(order_id, "refund.updated", refund):
                report.refunds_recovered += 1

    async def _replay(self, order_id: str, event_type: str, obj: dict[str, Any]) -> bool:
        """Applies a Stripe object as if its webhook had arrived. True if the order changed state."""
        async with self._sessions() as session, session.begin():
            before = await session.scalar(select(Order.status).where(Order.id == order_id))
            event = {"id": f"reconcile:{obj.get('id')}", "type": event_type, "data": {"object": obj}}
            await webhooks.apply_event(session, event, actor_type="system", actor_id=ACTOR)
            after = await session.scalar(select(Order.status).where(Order.id == order_id))
        if before != after:
            log.warning("reconciliation_applied", order_id=order_id, event_type=event_type, to=after)
        return before != after

    # ------------------------------------------------------------------ amounts
    async def _verify_payments(self, report: ReconciliationReport, since: datetime) -> None:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(Order.id, Order.stripe_payment_intent_id, Order.total_minor, Order.currency)
                    .where(
                        Order.status.in_(VERIFIABLE),
                        Order.reconciled_at.is_(None),
                        Order.stripe_payment_intent_id.is_not(None),
                        Order.updated_at >= since,
                    )
                    .limit(BATCH)
                )
            ).all()
        for order_id, payment_intent_id, total_minor, currency in rows:
            intent = await self._fetch(report, self._payments.retrieve_payment_intent(payment_intent_id))
            if intent is None:
                continue
            received = int(intent.get("amount_received") or 0)
            received_currency = str(intent.get("currency", "")).upper()
            matches = (
                intent.get("status") == "succeeded"
                and received == total_minor
                and received_currency == currency
            )
            async with self._sessions() as session, session.begin():
                order = await orders.load_for_update(session, order_id)
                order.reconciled_at = datetime.now(UTC)
                if matches:
                    report.orders_verified += 1
                    continue
                report.mismatches += 1
                await audit.record(
                    session,
                    actor_type="system",
                    actor_id=ACTOR,
                    action="reconciliation.mismatch",
                    entity_type="order",
                    entity_id=order.id,
                    before=orders.snapshot(order),
                    after={
                        "payment_intent": payment_intent_id,
                        "stripe_status": intent.get("status"),
                        "stripe_amount_received": received,
                        "stripe_currency": received_currency,
                    },
                )
                if order.status == "PAID":  # stop before it ships
                    await orders.transition(
                        session,
                        order,
                        "MANUAL_REVIEW",
                        actor_type="system",
                        actor_id=ACTOR,
                        reason="reconciliation_mismatch",
                    )
            await self._alerter.send(
                f"order:{order_id}:reconciliation_mismatch",
                "Order does not match what Stripe received",
                details={
                    "order_id": order_id,
                    "expected": _money(total_minor, currency),
                    "stripe_received": _money(received, received_currency),
                    "stripe_status": intent.get("status"),
                },
            )

    # ------------------------------------------------------------------ stuck orders
    async def _stuck_orders(self, report: ReconciliationReport, now: datetime) -> None:
        s = self._settings
        checks = [
            (
                "FULFILLMENT_FAILED",
                now - timedelta(minutes=s.reconciliation_grace_minutes),
                "refund_not_started",
                "Refund has not started for an order that could not be fulfilled",
            ),
            (
                "PAID",
                now - timedelta(minutes=s.fulfillment_stuck_after_minutes),
                "fulfillment_stuck",
                "Paid order has not been fulfilled",
            ),
        ]
        async with self._sessions() as session:
            for status, older_than, key, title in checks:
                rows = (
                    await session.execute(
                        select(Order.id, Order.updated_at, Order.fulfillment_requested_at)
                        .where(Order.status == status, Order.updated_at < older_than)
                        .limit(BATCH)
                    )
                ).all()
                for order_id, updated_at, requested_at in rows:
                    report.stuck_orders += 1
                    await self._alerter.send(
                        f"order:{order_id}:{key}",
                        title,
                        severity="warning",
                        details={
                            "order_id": order_id,
                            "status": status,
                            "since": updated_at.isoformat(),
                            "fulfillment_requested_at": requested_at.isoformat() if requested_at else None,
                        },
                    )

    # ------------------------------------------------------------------ system health
    async def _system_checks(self, report: ReconciliationReport) -> None:
        async with self._sessions() as session:
            report.dead_webhooks = int(
                await session.scalar(
                    select(func.count())
                    .select_from(WebhookEvent)
                    .where(
                        WebhookEvent.processed_at.is_(None), WebhookEvent.attempts >= webhooks.MAX_ATTEMPTS
                    )
                )
                or 0
            )
            report.audit_chain_ok, broken_at = await audit.verify_chain(session)
        if report.dead_webhooks:
            await self._alerter.send(
                "stripe:dead_webhooks",
                "Stripe webhook events failed every retry",
                details={"count": report.dead_webhooks},
            )
        if not report.audit_chain_ok:
            await self._alerter.send(
                "audit:chain_broken",
                "Audit log hash chain is broken: history may have been altered",
                details={"first_broken_row": broken_at},
            )
        if self._dead_letter_depth is not None:
            try:
                report.dead_letters = await self._dead_letter_depth()
            except Exception as exc:  # the broker being down must not stop reconciliation
                log.warning("dead_letter_check_failed", error=type(exc).__name__)
            if report.dead_letters:
                await self._alerter.send(
                    "events:dead_letters",
                    "Messages are waiting in the dead-letter queue",
                    severity="warning",
                    details={"queue": f"{self._settings.events_dlx}.q", "messages": report.dead_letters},
                )

    async def _fetch(
        self, report: ReconciliationReport, call: Awaitable[dict[str, Any]]
    ) -> dict[str, Any] | None:
        try:
            return await call
        except (PaymentTemporarilyUnavailable, PaymentRejected) as exc:
            report.stripe_errors += 1
            log.warning("reconciliation_stripe_error", error=str(exc))
            return None
