"""Explicit confirmation → order → Stripe Checkout (docs §6.2).

issue_confirmation : gateway asks for a single-use grant for ONE valid quote of ONE conversation
confirm            : the customer clicked "Confirm & pay" (gateway → here, never via the agent)
    1. TX: redeem the grant (single-use) + create order CREATED + audit
    2. commerce-svc: lock the quote + reserve stock/promotions      (idempotent per order)
    3. Stripe: create Checkout Session, key "pay:{order_id}:v1"      (idempotent per order)
    4. TX: order → AWAITING_PAYMENT + audit
A retry with the same Idempotency-Key resumes from wherever the previous attempt stopped.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from checkout_svc import audit, orders
from checkout_svc.commerce_client import CommerceClient, CommerceRejected, CommerceUnavailable
from checkout_svc.models import Confirmation, Order, new_id
from checkout_svc.payments import PaymentProvider, PaymentRejected, PaymentTemporarilyUnavailable
from checkout_svc.settings import CheckoutSettings
from commerce_common.errors import (
    Conflict,
    DomainError,
    Forbidden,
    NotFound,
    ServiceUnavailable,
    ValidationFailed,
)

log = structlog.get_logger("checkout")


class Gone(DomainError):
    http_status = 410


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# Stripe refund statuses → the three the widget shows (contracts.ts: OrderSummary.refund.status).
_REFUND_VIEW = {"succeeded": "succeeded", "failed": "failed", "canceled": "failed"}


def order_summary(order: Order) -> dict[str, Any]:
    """Wire shape the widget renders (widget/src/shared/api/contracts.ts: OrderSummary)."""
    return {
        "id": order.id,
        "status": order.status,
        "total": {"amount_minor": order.total_minor, "currency": order.currency},
        "checkout_url": order.checkout_url if order.status == "AWAITING_PAYMENT" else None,
        "refund": (
            {
                "amount": {"amount_minor": order.total_minor, "currency": order.currency},
                "status": _REFUND_VIEW.get(order.refund_status or "", "pending"),
            }
            if order.stripe_refund_id
            else None
        ),
        "updated_at": order.updated_at.isoformat(),
    }


class CheckoutService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: CheckoutSettings,
        commerce: CommerceClient,
        payments: PaymentProvider,
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._commerce = commerce
        self._payments = payments

    # --------------------------------------------------------------------- confirmation grant
    async def issue_confirmation(self, quote_id: str, conversation_id: str) -> dict[str, Any]:
        try:
            quote = await self._commerce.get_quote(quote_id)
            cart = await self._commerce.get_cart(quote["cart_id"])
        except CommerceRejected as exc:
            raise NotFound("quote_not_found", "Order summary not found") from exc
        except CommerceUnavailable as exc:
            raise ServiceUnavailable("commerce_unavailable", "Please try again in a moment") from exc
        if cart["conversation_id"] != conversation_id:
            raise Forbidden("forbidden", "This order summary belongs to another conversation")
        if not quote["valid"]:
            raise Conflict(
                "quote_invalid", f"This order summary is no longer valid ({quote['invalid_reason']})"
            )
        total = int(quote["total"]["amount_minor"])
        if total > self._settings.max_order_total_minor:
            raise ValidationFailed("order_limit_exceeded", "This order exceeds the maximum allowed amount")

        token = secrets.token_urlsafe(32)
        expires_at = min(
            datetime.now(UTC) + timedelta(minutes=self._settings.confirmation_token_ttl_minutes),
            datetime.fromisoformat(quote["expires_at"]),
        )
        async with self._sessions() as session, session.begin():
            session.add(
                Confirmation(
                    id=new_id("cfm"),
                    token_hash=token_hash(token),
                    quote_id=quote_id,
                    quote_hash=quote["hash"],
                    conversation_id=conversation_id,
                    amount_minor=total,
                    currency=quote["total"]["currency"],
                    expires_at=expires_at,
                )
            )
        return {"token": token, "expires_at": expires_at.isoformat()}

    # --------------------------------------------------------------------------- confirm
    async def confirm(self, token: str, conversation_id: str, idempotency_key: str) -> dict[str, Any]:
        order_id = await self._redeem(token, conversation_id, idempotency_key)
        return await self._advance(order_id)

    async def _redeem(self, token: str, conversation_id: str, idempotency_key: str) -> str:
        async with self._sessions() as session, session.begin():
            # Same click retried (network blip, double request): resume the SAME order.
            existing = (
                await session.execute(select(Order).where(Order.idempotency_key == idempotency_key))
            ).scalar_one_or_none()
            if existing is not None:
                if existing.conversation_id != conversation_id:
                    raise Forbidden("forbidden", "Idempotency key belongs to another conversation")
                return existing.id

            grant = (
                await session.execute(
                    select(Confirmation).where(Confirmation.token_hash == token_hash(token)).with_for_update()
                )
            ).scalar_one_or_none()
            if grant is None:
                raise NotFound("confirmation_not_found", "This payment link is not valid")
            if grant.conversation_id != conversation_id:
                raise Forbidden("forbidden", "This payment link belongs to another conversation")
            if grant.used_at is not None:
                raise Conflict("confirmation_used", "This order was already confirmed")
            if grant.expires_at <= datetime.now(UTC):
                raise Gone(
                    "confirmation_expired", "This price has expired. Please refresh your order summary."
                )

            order = Order(
                id=new_id("ord"),
                conversation_id=conversation_id,
                confirmation_id=grant.id,
                quote_id=grant.quote_id,
                quote_hash=grant.quote_hash,
                idempotency_key=idempotency_key,
                currency=grant.currency,
                total_minor=grant.amount_minor,
                status="CREATED",
                version=1,
            )
            session.add(order)
            grant.used_at = datetime.now(UTC)
            grant.order_id = order.id
            await session.flush()
            await audit.record(
                session,
                actor_type="user",
                actor_id=conversation_id,
                action="order.created",
                entity_type="order",
                entity_id=order.id,
                after={**orders.snapshot(order), "quote_id": order.quote_id, "confirmation_id": grant.id},
            )
            log.info(
                "order_created", order_id=order.id, total_minor=order.total_minor, currency=order.currency
            )
            return order.id

    async def _advance(self, order_id: str) -> dict[str, Any]:
        async with self._sessions() as session:
            order = await session.get(Order, order_id)
            if order is None:
                raise NotFound("order_not_found", "Order not found")
            if order.status == "AWAITING_PAYMENT":
                return {"order_id": order.id, "checkout_url": order.checkout_url}  # idempotent replay
            if order.status != "CREATED":
                raise Conflict("order_not_payable", f"This order is {order.status.lower().replace('_', ' ')}")

        # Step 2: lock the quote and reserve stock/promotions in commerce-svc.
        try:
            quote = await self._commerce.lock_quote(order.quote_id, order.id)
        except CommerceRejected as exc:
            await self._cancel(order.id, exc.code, settlement="release")
            raise Conflict(exc.code, exc.message) from exc
        except CommerceUnavailable as exc:
            raise ServiceUnavailable("commerce_unavailable", "Please try again in a moment") from exc

        # Defense in depth: the locked quote must be exactly what the customer was shown and confirmed.
        if quote["hash"] != order.quote_hash or int(quote["total"]["amount_minor"]) != order.total_minor:
            await self._cancel(order.id, "quote_mismatch", settlement="release")
            log.error("quote_mismatch", order_id=order.id)
            raise Conflict("quote_mismatch", "The order summary changed. Please review it again.")

        # Step 3: Stripe Checkout Session (deterministic idempotency key: a retry returns the same session).
        try:
            result = await self._payments.create_checkout_session(
                order_id=order.id,
                conversation_id=order.conversation_id,
                quote=quote,
                idempotency_key=f"pay:{order.id}:v1",
            )
        except PaymentTemporarilyUnavailable as exc:
            raise ServiceUnavailable(
                "payment_unavailable", "Payment provider is busy. Please try again."
            ) from exc
        except PaymentRejected as exc:
            log.error("payment_rejected", order_id=order.id, error=str(exc))
            await self._cancel(order.id, "payment_provider_rejected", settlement="release")
            raise Conflict(
                "payment_unavailable", "We couldn't start payment. Your card was not charged."
            ) from exc

        # Step 4
        async with self._sessions() as session, session.begin():
            locked = await orders.load_for_update(session, order.id)
            if locked.status == "CREATED":
                await orders.transition(
                    session,
                    locked,
                    "AWAITING_PAYMENT",
                    actor_type="system",
                    actor_id="checkout-svc",
                    stripe_checkout_session_id=result.session_id,
                    checkout_url=result.url,
                )
            url = locked.checkout_url
        log.info("checkout_session_created", order_id=order.id)
        return {"order_id": order.id, "checkout_url": url}

    async def _cancel(self, order_id: str, reason: str, *, settlement: str | None) -> None:
        async with self._sessions() as session, session.begin():
            order = await orders.load_for_update(session, order_id)
            if orders.can_transition(order, "CANCELED"):
                await orders.transition(
                    session,
                    order,
                    "CANCELED",
                    actor_type="system",
                    actor_id="checkout-svc",
                    reason=reason,
                    settlement=settlement,
                )

    # ---------------------------------------------------------------------------- reads
    async def get_order(self, order_id: str, conversation_id: str | None) -> dict[str, Any]:
        async with self._sessions() as session:
            order = await session.get(Order, order_id)
        if order is None or (conversation_id is not None and order.conversation_id != conversation_id):
            raise NotFound("order_not_found", "Order not found")  # never reveal other customers' orders
        return order_summary(order)

    async def conversation_orders(self, conversation_id: str, limit: int = 5) -> list[dict[str, Any]]:
        async with self._sessions() as session:
            rows = await session.execute(
                select(Order)
                .where(Order.conversation_id == conversation_id)
                .order_by(Order.created_at.desc())
                .limit(limit)
            )
            return [order_summary(o) for o in rows.scalars()]
