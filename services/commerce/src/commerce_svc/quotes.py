"""Quotes: the immutable, hashed price snapshot that checkout will charge exactly (docs §6.1–6.2)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from commerce_common.errors import NotFound, ValidationFailed
from commerce_common.metrics import counter, prime_labels
from commerce_common.money import MoneyDTO
from commerce_svc.carts import CartService
from commerce_svc.models import Cart, Quote, new_id
from commerce_svc.pricing.breakdown import Breakdown, compute
from commerce_svc.pricing.rules import PromotionRepository, StaticTaxProvider, shipping_rule
from commerce_svc.schemas import DiscountDTO, QuoteDTO, QuoteLineDTO
from commerce_svc.settings import CommerceSettings


def quote_hash(content: dict[str, Any]) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _content(
    quote_id: str, cart: Cart, b: Breakdown, fx_rate_id: str | None, expires_at: datetime
) -> dict[str, Any]:
    """Everything the customer agrees to. Hashed, stored, and never recomputed."""
    return {
        "id": quote_id,
        "cart_id": cart.id,
        "cart_version": cart.version,
        "currency": b.currency,
        "lines": [
            QuoteLineDTO(
                sku_id=r.line.sku_id,
                name=r.line.name,
                variant_label=r.line.variant_label,
                quantity=r.line.quantity,
                unit_price=MoneyDTO.of(r.line.unit_price),
                line_total=MoneyDTO.of(r.gross),
                discount=MoneyDTO.of(r.discount),
                tax=MoneyDTO.of(r.tax),
            ).model_dump(mode="json")
            for r in b.lines
        ],
        "subtotal": MoneyDTO.of(b.subtotal).model_dump(),
        "discounts": [
            DiscountDTO(code=d.code, label=d.label, amount=MoneyDTO.of(d.amount)).model_dump(mode="json")
            for d in b.discounts
        ],
        "shipping": MoneyDTO.of(b.shipping).model_dump(),
        "tax_total": MoneyDTO.of(b.tax_total).model_dump(),
        "tax_inclusive": b.tax_inclusive,
        "total": MoneyDTO.of(b.total).model_dump(),
        "fx_rate_id": fx_rate_id,
        "promotion_ids": [d.promotion_id for d in b.discounts],  # redeemed against caps when locked
        "destination": {"country": cart.ship_country, "region": cart.ship_region},
        "expires_at": expires_at.isoformat(),
    }


QUOTES_CREATED = prime_labels(counter("commerce.quotes.created", "Order summaries (quotes) issued"))


class QuoteService:
    def __init__(self, session: AsyncSession, settings: CommerceSettings, carts: CartService) -> None:
        self._session = session
        self._settings = settings
        self._carts = carts
        self._promos = PromotionRepository(session, settings.default_merchant_id)
        self._tax = StaticTaxProvider(session, settings)

    async def create(self, cart_id: str) -> QuoteDTO:
        QUOTES_CREATED.add(1)
        cart = await self._carts.load(cart_id, for_update=True)  # serialise with cart mutations
        if not cart.items:
            raise ValidationFailed("cart_empty", "Add something to the cart before checking out")
        if not cart.ship_country:
            raise ValidationFailed(
                "shipping_address_required", "A shipping address is needed to calculate tax"
            )

        stock = await self._carts.available_stock([i.sku_id for i in cart.items])
        short = [i.sku_id for i in cart.items if stock.get(i.sku_id, 0) < i.quantity]
        if short:
            raise ValidationFailed(
                "insufficient_stock", "Some items are no longer in stock", details={"sku_ids": short}
            )

        lines, fx_rate_id = await self._carts.priced_lines(cart)
        breakdown = compute(
            lines,
            await self._promos.rules_for_cart(cart.promo_code),
            cart.currency,
            shipping=await shipping_rule(self._session, cart.currency),
            tax=await self._tax.rule_for(cart.ship_country, cart.ship_region),
        )

        # A cart has at most one open quote.
        await self._session.execute(
            update(Quote)
            .where(Quote.cart_id == cart.id, Quote.status == "active")
            .values(status="superseded")
        )
        quote_id = new_id("quote")
        expires_at = (datetime.now(UTC) + timedelta(minutes=self._settings.quote_ttl_minutes)).replace(
            microsecond=0
        )
        content = _content(quote_id, cart, breakdown, fx_rate_id, expires_at)
        quote = Quote(
            id=quote_id,
            cart_id=cart.id,
            merchant_id=cart.merchant_id,
            cart_version=cart.version,
            status="active",
            currency=cart.currency,
            subtotal_minor=breakdown.subtotal.amount_minor,
            discount_minor=breakdown.discount_total.amount_minor,
            shipping_minor=breakdown.shipping.amount_minor,
            tax_minor=breakdown.tax_total.amount_minor,
            total_minor=breakdown.total.amount_minor,
            tax_inclusive=breakdown.tax_inclusive,
            fx_rate_id=fx_rate_id,
            payload=content,
            hash=quote_hash(content),
            expires_at=expires_at,
        )
        self._session.add(quote)
        await self._session.flush()
        return self.to_dto(quote, cart)

    async def get(self, quote_id: str) -> QuoteDTO:
        quote = await self._session.get(Quote, quote_id)
        if quote is None or quote.merchant_id != self._settings.default_merchant_id:
            raise NotFound("quote_not_found", f"Quote {quote_id} not found")
        return self.to_dto(quote, await self._carts.load(quote.cart_id))

    @staticmethod
    def to_dto(quote: Quote, cart: Cart) -> QuoteDTO:
        reason: str | None = None
        if quote.status != "active":
            reason = quote.status
        elif quote.expires_at <= datetime.now(UTC):
            reason = "expired"
        elif cart.version != quote.cart_version:
            reason = "cart_changed"
        p = quote.payload
        return QuoteDTO(
            id=quote.id,
            cart_id=quote.cart_id,
            cart_version=quote.cart_version,
            status=quote.status,
            valid=reason is None,
            invalid_reason=reason,
            currency=quote.currency,
            lines=[QuoteLineDTO.model_validate(line) for line in p["lines"]],
            subtotal=MoneyDTO.model_validate(p["subtotal"]),
            discounts=[DiscountDTO.model_validate(d) for d in p["discounts"]],
            shipping=MoneyDTO.model_validate(p["shipping"]),
            tax_total=MoneyDTO.model_validate(p["tax_total"]),
            tax_inclusive=p["tax_inclusive"],
            total=MoneyDTO.model_validate(p["total"]),
            fx_rate_id=quote.fx_rate_id,
            hash=quote.hash,
            expires_at=quote.expires_at,
        )


async def expire_stale_quotes(session: AsyncSession) -> int:
    result = await session.execute(
        update(Quote)
        .where(Quote.status == "active", Quote.expires_at <= datetime.now(UTC))
        .values(status="expired")
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
