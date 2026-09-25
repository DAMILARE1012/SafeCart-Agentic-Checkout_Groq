"""Quote locking, stock reservation and promotion redemption for an order (docs §6.2, §7.4).

Called only by checkout-svc. Each operation is ONE local transaction and idempotent per
``order_id``, so checkout can retry any step after a crash without double-reserving,
double-committing or double-releasing.

    lock(quote, order)  : quote active → locked; stock reserved (TTL); promo uses reserved
    commit(order)       : payment confirmed → stock leaves on_hand; promo uses final; cart emptied
    release(order)      : payment failed/expired/cancelled → stock and promo uses given back
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from commerce_common.errors import Conflict, NotFound
from commerce_svc.models import (
    Cart,
    CartItem,
    InventoryReservation,
    Promotion,
    PromotionRedemption,
    Quote,
    new_id,
)
from commerce_svc.settings import CommerceSettings

_RESERVE_SQL = text(
    """
    UPDATE inventory SET reserved = reserved + :qty
    WHERE sku_id = :sku AND on_hand - reserved >= :qty
    RETURNING sku_id
    """
)
_REDEEM_SQL = text(
    """
    UPDATE promotions SET redemption_count = redemption_count + 1
    WHERE id = :pid AND active AND (max_redemptions IS NULL OR redemption_count < max_redemptions)
    RETURNING id
    """
)


class ReservationService:
    def __init__(self, session: AsyncSession, settings: CommerceSettings) -> None:
        self._session = session
        self._settings = settings
        self._merchant_id = settings.default_merchant_id

    # ------------------------------------------------------------------------------ lock
    async def lock(self, quote_id: str, order_id: str) -> Quote:
        quote = (
            await self._session.execute(
                select(Quote)
                .where(Quote.id == quote_id, Quote.merchant_id == self._merchant_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if quote is None:
            raise NotFound("quote_not_found", f"Quote {quote_id} not found")

        if quote.status == "locked":
            if quote.locked_order_id == order_id:
                return quote  # idempotent retry
            raise Conflict("quote_already_used", "This order summary was already confirmed")
        if quote.status != "active":
            raise Conflict("quote_invalid", f"This order summary is no longer valid ({quote.status})")
        if quote.expires_at <= datetime.now(UTC):
            raise Conflict("quote_expired", "This price has expired. Please refresh your order summary.")
        cart = (
            await self._session.execute(select(Cart).where(Cart.id == quote.cart_id).with_for_update())
        ).scalar_one()
        if cart.version != quote.cart_version:
            raise Conflict("cart_changed", "Your cart changed after this summary was created")

        # Reserve stock. Sorted by SKU so concurrent locks take row locks in the same order (no deadlocks).
        # Any shortfall raises → the whole transaction (including earlier reservations) rolls back.
        quantities: dict[str, int] = defaultdict(int)
        for line in quote.payload["lines"]:
            quantities[line["sku_id"]] += int(line["quantity"])
        expires_at = datetime.now(UTC) + timedelta(minutes=self._settings.inventory_reservation_ttl_minutes)
        for sku_id in sorted(quantities):
            reserved = (
                await self._session.execute(_RESERVE_SQL, {"sku": sku_id, "qty": quantities[sku_id]})
            ).first()
            if reserved is None:
                raise Conflict(
                    "insufficient_stock", "Some items sold out before payment", details={"sku_id": sku_id}
                )
            self._session.add(
                InventoryReservation(
                    id=new_id("res"),
                    order_id=order_id,
                    sku_id=sku_id,
                    quantity=quantities[sku_id],
                    status="reserved",
                    expires_at=expires_at,
                )
            )

        for promotion_id in await self._promotion_ids(quote):
            if (await self._session.execute(_REDEEM_SQL, {"pid": promotion_id})).first() is None:
                raise Conflict("promo_exhausted", "A promotion in this order is no longer available")
            self._session.add(
                PromotionRedemption(
                    id=new_id("red"), order_id=order_id, promotion_id=promotion_id, status="reserved"
                )
            )

        quote.status = "locked"
        quote.locked_order_id = order_id
        await self._session.flush()
        return quote

    async def _promotion_ids(self, quote: Quote) -> list[str]:
        ids: list[str] = list(quote.payload.get("promotion_ids", []))
        if ids:
            return ids
        # Quotes created before promotion ids were recorded: resolve by code.
        codes = [d["code"] for d in quote.payload.get("discounts", []) if d.get("code")]
        if not codes:
            return []
        rows = await self._session.execute(
            select(Promotion.id).where(Promotion.merchant_id == self._merchant_id, Promotion.code.in_(codes))
        )
        return list(rows.scalars())

    # ---------------------------------------------------------------------------- commit
    async def commit(self, order_id: str) -> dict[str, Any]:
        """Payment confirmed. Idempotent: already-committed rows are skipped.

        A reservation that already expired (payment completed very late) is still honoured by taking
        the stock now; if that stock is gone, the SKU is reported as oversold so fulfilment can
        compensate (refund) instead of silently shipping nothing.
        """
        reservations = await self._reservations(order_id, lock=True)
        oversold: list[str] = []
        for r in reservations:
            if r.status == "reserved":
                await self._session.execute(
                    text(
                        "UPDATE inventory SET on_hand = on_hand - :q, reserved = reserved - :q "
                        "WHERE sku_id = :s"
                    ),
                    {"q": r.quantity, "s": r.sku_id},
                )
            elif r.status == "released":
                taken = (
                    await self._session.execute(
                        text(
                            "UPDATE inventory SET on_hand = on_hand - :q "
                            "WHERE sku_id = :s AND on_hand - reserved >= :q RETURNING sku_id"
                        ),
                        {"q": r.quantity, "s": r.sku_id},
                    )
                ).first()
                if taken is None:
                    oversold.append(r.sku_id)
            else:
                continue
            r.status = "committed"

        await self._session.execute(
            update(PromotionRedemption)
            .where(PromotionRedemption.order_id == order_id, PromotionRedemption.status == "reserved")
            .values(status="committed")
        )
        await self._empty_cart_for(order_id)
        await self._session.flush()
        return {"order_id": order_id, "committed": True, "oversold_sku_ids": oversold}

    async def _empty_cart_for(self, order_id: str) -> None:
        """The order now owns those items: start the conversation's next purchase from an empty cart."""
        quote = (
            await self._session.execute(select(Quote).where(Quote.locked_order_id == order_id))
        ).scalar_one_or_none()
        if quote is None:
            return
        cart = (
            await self._session.execute(select(Cart).where(Cart.id == quote.cart_id).with_for_update())
        ).scalar_one_or_none()
        if cart is None or cart.version != quote.cart_version:
            return  # customer already changed the cart after confirming: leave their new cart alone
        await self._session.execute(delete(CartItem).where(CartItem.cart_id == cart.id))
        cart.promo_code = None
        cart.version += 1
        cart.updated_at = datetime.now(UTC)

    # --------------------------------------------------------------------------- release
    async def release(self, order_id: str) -> dict[str, Any]:
        """Payment failed / expired / cancelled. Idempotent: only 'reserved' rows are released."""
        released = 0
        for r in await self._reservations(order_id, lock=True):
            if r.status != "reserved":
                continue
            await self._session.execute(
                text("UPDATE inventory SET reserved = reserved - :q WHERE sku_id = :s"),
                {"q": r.quantity, "s": r.sku_id},
            )
            r.status = "released"
            released += 1

        redemptions = (
            await self._session.execute(
                select(PromotionRedemption)
                .where(PromotionRedemption.order_id == order_id, PromotionRedemption.status == "reserved")
                .with_for_update()
            )
        ).scalars()
        for redemption in redemptions:
            await self._session.execute(
                text(
                    "UPDATE promotions SET redemption_count = GREATEST(redemption_count - 1, 0) WHERE id = :p"
                ),
                {"p": redemption.promotion_id},
            )
            redemption.status = "released"

        await self._session.execute(
            update(Quote)
            .where(Quote.locked_order_id == order_id, Quote.status == "locked")
            .values(status="released")
        )
        await self._session.flush()
        return {"order_id": order_id, "released_reservations": released}

    async def _reservations(self, order_id: str, *, lock: bool) -> list[InventoryReservation]:
        stmt = select(InventoryReservation).where(InventoryReservation.order_id == order_id)
        if lock:
            stmt = stmt.order_by(InventoryReservation.sku_id).with_for_update()
        return list((await self._session.execute(stmt)).scalars())


async def release_expired_reservations(session: AsyncSession, settings: CommerceSettings) -> int:
    """Worker safety net: if checkout never reported an outcome, give the stock back after the TTL."""
    rows = await session.execute(
        select(InventoryReservation.order_id)
        .where(
            InventoryReservation.status == "reserved", InventoryReservation.expires_at <= datetime.now(UTC)
        )
        .distinct()
        .limit(100)
    )
    service = ReservationService(session, settings)
    order_ids = list(rows.scalars())
    for order_id in order_ids:
        await service.release(order_id)
    return len(order_ids)
