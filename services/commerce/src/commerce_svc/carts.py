"""Carts: every mutation is validated here, bumps the cart version and supersedes open quotes."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from commerce_common.errors import NotFound, ValidationFailed
from commerce_common.money import MoneyDTO
from commerce_svc.crypto import AddressCipher
from commerce_svc.models import Cart, CartItem, Inventory, Quote, Sku, new_id
from commerce_svc.pricing.breakdown import Breakdown, PricedLine, RejectedPromotion, compute
from commerce_svc.pricing.resolver import PriceResolver
from commerce_svc.pricing.rules import PromotionRepository, to_rule
from commerce_svc.schemas import (
    CartLineDTO,
    CartSnapshotDTO,
    DestinationDTO,
    DiscountDTO,
    PromoIssueDTO,
    ShippingAddressRequest,
)
from commerce_svc.settings import CommerceSettings


def _promo_message(rejection: RejectedPromotion, code: str) -> str:
    if rejection.reason == "min_subtotal_not_met" and rejection.min_subtotal is not None:
        return f"{code} needs a subtotal of at least {rejection.min_subtotal}"
    if rejection.reason == "currency_mismatch":
        return f"{code} can't be used with this cart's currency"
    return f"{code} doesn't reduce this cart's total"


class CartService:
    def __init__(self, session: AsyncSession, settings: CommerceSettings, cipher: AddressCipher) -> None:
        self._session = session
        self._settings = settings
        self._merchant_id = settings.default_merchant_id
        self._cipher = cipher
        self._prices = PriceResolver(session, settings)
        self._promos = PromotionRepository(session, self._merchant_id)

    # ------------------------------------------------------------------ loading
    async def get_or_create(self, conversation_id: str, currency: str | None) -> Cart:
        currency = currency or self._settings.default_currency
        if currency not in self._settings.supported_currencies:
            raise ValidationFailed("currency_not_supported", f"{currency} is not supported")
        existing = await self._find(conversation_id)
        if existing is not None:
            return existing  # a cart's currency is fixed at creation
        cart = Cart(
            id=new_id("cart"),
            merchant_id=self._merchant_id,
            conversation_id=conversation_id,
            currency=currency,
            version=1,
        )
        self._session.add(cart)
        await self._session.flush()
        return await self.load(cart.id)

    async def _find(self, conversation_id: str) -> Cart | None:
        stmt = (
            select(Cart)
            .where(Cart.merchant_id == self._merchant_id, Cart.conversation_id == conversation_id)
            .options(selectinload(Cart.items))
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def load(self, cart_id: str, *, for_update: bool = False) -> Cart:
        """``for_update`` row-locks the cart so concurrent mutations/quotes serialise."""
        stmt = select(Cart).where(Cart.id == cart_id, Cart.merchant_id == self._merchant_id)
        if for_update:
            stmt = stmt.with_for_update()
        cart = (
            await self._session.execute(
                stmt.options(selectinload(Cart.items)).execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if cart is None:
            raise NotFound("cart_not_found", f"Cart {cart_id} not found")
        return cart

    # ---------------------------------------------------------------- mutations
    async def add_item(self, cart_id: str, sku_id: str, quantity: int) -> Cart:
        cart = await self.load(cart_id, for_update=True)
        existing = next((i for i in cart.items if i.sku_id == sku_id), None)
        new_qty = quantity + (existing.quantity if existing else 0)
        await self._check_sellable(sku_id, new_qty, cart.currency)
        if existing:
            existing.quantity = new_qty
        else:
            cart.items.append(CartItem(id=new_id("line"), cart_id=cart.id, sku_id=sku_id, quantity=new_qty))
        return await self._touch(cart)

    async def set_quantity(self, cart_id: str, item_id: str, quantity: int) -> Cart:
        cart = await self.load(cart_id, for_update=True)
        item = self._item(cart, item_id)
        if quantity == 0:
            cart.items.remove(item)
        else:
            await self._check_sellable(item.sku_id, quantity, cart.currency)
            item.quantity = quantity
        return await self._touch(cart)

    async def remove_item(self, cart_id: str, item_id: str) -> Cart:
        return await self.set_quantity(cart_id, item_id, 0)

    async def apply_promo(self, cart_id: str, code: str) -> Cart:
        code = code.strip().upper()
        cart = await self.load(cart_id, for_update=True)
        promo = await self._promos.find_code(code)
        lines, _ = await self.priced_lines(cart)
        breakdown = compute(lines, [to_rule(promo)], cart.currency)
        if breakdown.rejected:
            rejection = breakdown.rejected[0]
            raise ValidationFailed(f"promo_{rejection.reason}", _promo_message(rejection, code))
        cart.promo_code = code
        return await self._touch(cart)

    async def remove_promo(self, cart_id: str) -> Cart:
        cart = await self.load(cart_id, for_update=True)
        cart.promo_code = None
        return await self._touch(cart)

    async def set_shipping_address(self, cart_id: str, address: ShippingAddressRequest) -> Cart:
        cart = await self.load(cart_id, for_update=True)
        region = address.region.upper() if address.region else None
        cart.shipping_address_enc = self._cipher.encrypt(address.model_dump())
        cart.ship_country = address.country
        cart.ship_region = region
        return await self._touch(cart)

    # ------------------------------------------------------------------ pricing
    async def priced_lines(self, cart: Cart) -> tuple[list[PricedLine], str | None]:
        """Current prices for the cart's items (plus the FX snapshot used, if any)."""
        sku_ids = [i.sku_id for i in cart.items]
        prices = await self._prices.resolve(sku_ids, cart.currency)
        skus = {
            s.id: s
            for s in (
                await self._session.execute(
                    select(Sku).where(Sku.id.in_(sku_ids)).options(selectinload(Sku.product))
                )
            ).scalars()
        }
        lines: list[PricedLine] = []
        fx_rate_id: str | None = None
        for item in cart.items:
            price = prices.get(item.sku_id)
            sku = skus.get(item.sku_id)
            if price is None or sku is None:
                raise ValidationFailed("price_unavailable", f"Item {item.sku_id} can no longer be priced")
            fx_rate_id = fx_rate_id or price.fx_rate_id
            lines.append(
                PricedLine(
                    key=item.id,
                    sku_id=sku.id,
                    product_id=sku.product_id,
                    name=sku.product.name,
                    variant_label=sku.variant_label,
                    image_url=sku.image_url,
                    quantity=item.quantity,
                    unit_price=price.unit,
                    compare_at=price.compare_at,
                )
            )
        return lines, fx_rate_id

    async def breakdown(self, cart: Cart) -> Breakdown:
        lines, _ = await self.priced_lines(cart)
        rules = await self._promos.rules_for_cart(cart.promo_code)
        return compute(lines, rules, cart.currency)

    async def snapshot(self, cart: Cart) -> CartSnapshotDTO:
        b = await self.breakdown(cart)
        issue: PromoIssueDTO | None = None
        if cart.promo_code:
            rejection = next((r for r in b.rejected if r.code == cart.promo_code), None)
            applied = any(d.code == cart.promo_code for d in b.discounts)
            if rejection is not None:
                issue = PromoIssueDTO(
                    code=cart.promo_code,
                    reason=rejection.reason,
                    message=_promo_message(rejection, cart.promo_code),
                )
            elif not applied:
                issue = PromoIssueDTO(
                    code=cart.promo_code,
                    reason="no_longer_valid",
                    message=f"{cart.promo_code} is no longer valid",
                )
        return CartSnapshotDTO(
            id=cart.id,
            conversation_id=cart.conversation_id,
            currency=cart.currency,
            version=cart.version,
            lines=[
                CartLineDTO(
                    id=r.line.key,
                    sku_id=r.line.sku_id,
                    product_id=r.line.product_id,
                    name=r.line.name,
                    variant_label=r.line.variant_label,
                    image_url=r.line.image_url,
                    quantity=r.line.quantity,
                    unit_price=MoneyDTO.of(r.line.unit_price),
                    line_total=MoneyDTO.of(r.gross),
                )
                for r in b.lines
            ],
            item_count=b.item_count,
            subtotal=MoneyDTO.of(b.subtotal),
            discounts=[
                DiscountDTO(code=d.code, label=d.label, amount=MoneyDTO.of(d.amount)) for d in b.discounts
            ],
            discount_total=MoneyDTO.of(b.discount_total),
            total_after_discounts=MoneyDTO.of(b.subtotal - b.discount_total),
            promo_code=cart.promo_code,
            promo_issue=issue,
            shipping_destination=(
                DestinationDTO(country=cart.ship_country, region=cart.ship_region)
                if cart.ship_country
                else None
            ),
        )

    # ------------------------------------------------------------------ helpers
    async def available_stock(self, sku_ids: list[str]) -> dict[str, int]:
        rows = await self._session.execute(select(Inventory).where(Inventory.sku_id.in_(sku_ids)))
        return {inv.sku_id: inv.available for inv in rows.scalars()}

    async def _check_sellable(self, sku_id: str, quantity: int, currency: str) -> None:
        if quantity > self._settings.max_item_quantity:
            raise ValidationFailed(
                "quantity_limit", f"You can buy at most {self._settings.max_item_quantity} of an item"
            )
        sku = (
            await self._session.execute(
                select(Sku).where(Sku.id == sku_id).options(selectinload(Sku.product))
            )
        ).scalar_one_or_none()
        if (
            sku is None
            or not sku.active
            or not sku.product.active
            or sku.product.merchant_id != self._merchant_id
        ):
            raise NotFound("sku_not_found", f"Item {sku_id} is not available")
        available = (await self.available_stock([sku_id])).get(sku_id, 0)
        if available < quantity:
            raise ValidationFailed(
                "insufficient_stock",
                f"Only {available} left of {sku.product.name}"
                if available
                else f"{sku.product.name} is out of stock",
                details={"available": available},
            )
        if sku_id not in await self._prices.resolve([sku_id], currency):
            raise ValidationFailed("price_unavailable", f"{sku.product.name} can't be sold in {currency}")

    @staticmethod
    def _item(cart: Cart, item_id: str) -> CartItem:
        item = next((i for i in cart.items if i.id == item_id), None)
        if item is None:
            raise NotFound("cart_item_not_found", f"Cart line {item_id} not found")
        return item

    async def _touch(self, cart: Cart) -> Cart:
        """New version → any open quote no longer reflects the cart."""
        cart.version += 1
        cart.updated_at = datetime.now(UTC)
        await self._session.execute(
            update(Quote)
            .where(Quote.cart_id == cart.id, Quote.status == "active")
            .values(status="superseded")
        )
        await self._session.flush()
        return await self.load(cart.id)
