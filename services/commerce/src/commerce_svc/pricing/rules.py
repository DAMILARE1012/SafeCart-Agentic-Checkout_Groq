"""Loads promotion, shipping and tax rules from the database into pure rule objects."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from commerce_common.errors import ValidationFailed
from commerce_common.money import Money
from commerce_svc.models import Promotion, ShippingRate, TaxRate
from commerce_svc.settings import CommerceSettings

from .breakdown import PromotionRule, ShippingRule, TaxRule


def to_rule(promo: Promotion) -> PromotionRule:
    currency = promo.currency
    return PromotionRule(
        promotion_id=promo.id,
        code=promo.code,
        label=promo.label,
        kind=cast(Literal["percent", "fixed"], promo.kind),
        percent_bps=promo.percent_bps,
        amount=Money(promo.amount_minor, currency) if promo.amount_minor is not None and currency else None,
        min_subtotal=(
            Money(promo.min_subtotal_minor, currency)
            if promo.min_subtotal_minor is not None and currency
            else None
        ),
    )


def _live(now: datetime) -> list[ColumnElement[bool]]:
    return [
        Promotion.active.is_(True),
        or_(Promotion.starts_at.is_(None), Promotion.starts_at <= now),
        or_(Promotion.ends_at.is_(None), Promotion.ends_at > now),
        or_(Promotion.max_redemptions.is_(None), Promotion.redemption_count < Promotion.max_redemptions),
    ]


class PromotionRepository:
    def __init__(self, session: AsyncSession, merchant_id: str) -> None:
        self._session = session
        self._merchant_id = merchant_id

    async def rules_for_cart(self, promo_code: str | None) -> list[PromotionRule]:
        """Automatic promotions first, then the cart's code (if still live)."""
        now = datetime.now(UTC)
        stmt = select(Promotion).where(Promotion.merchant_id == self._merchant_id, *_live(now))
        stmt = stmt.where(
            or_(Promotion.code.is_(None), Promotion.code == promo_code)
            if promo_code
            else Promotion.code.is_(None)
        )
        promos = list(
            (await self._session.execute(stmt.order_by(Promotion.code.nulls_first(), Promotion.id))).scalars()
        )
        return [to_rule(p) for p in promos]

    async def find_code(self, code: str) -> Promotion:
        """Looks up a code and explains precisely why it can't be used (the agent relays this)."""
        promo = (
            await self._session.execute(
                select(Promotion).where(Promotion.merchant_id == self._merchant_id, Promotion.code == code)
            )
        ).scalar_one_or_none()
        if promo is None or not promo.active:
            raise ValidationFailed("promo_not_found", f"Promo code {code} is not valid")
        now = datetime.now(UTC)
        if promo.starts_at and promo.starts_at > now:
            raise ValidationFailed("promo_not_started", f"Promo code {code} is not active yet")
        if promo.ends_at and promo.ends_at <= now:
            raise ValidationFailed("promo_expired", f"Promo code {code} has expired")
        if promo.max_redemptions is not None and promo.redemption_count >= promo.max_redemptions:
            raise ValidationFailed("promo_exhausted", f"Promo code {code} has been fully redeemed")
        return promo

    async def advertised(self) -> list[Promotion]:
        now = datetime.now(UTC)
        stmt = select(Promotion).where(
            Promotion.merchant_id == self._merchant_id, Promotion.advertised.is_(True), *_live(now)
        )
        return list((await self._session.execute(stmt.order_by(Promotion.label))).scalars())


async def shipping_rule(session: AsyncSession, currency: str) -> ShippingRule:
    rate = await session.get(ShippingRate, currency)
    if rate is None:
        raise ValidationFailed("shipping_unavailable", f"Shipping is not configured for {currency}")
    free_from = Money(rate.free_from_minor, currency) if rate.free_from_minor is not None else None
    return ShippingRule(flat=Money(rate.flat_minor, currency), free_from=free_from)


class TaxProvider(Protocol):
    async def rule_for(self, country: str, region: str | None) -> TaxRule: ...


class StaticTaxProvider:
    """Rates from the ``tax_rates`` table: (country, region) first, then country-wide, else zero."""

    def __init__(self, session: AsyncSession, settings: CommerceSettings) -> None:
        self._session = session
        self._default_inclusive = settings.tax_default_behavior == "inclusive"

    async def rule_for(self, country: str, region: str | None) -> TaxRule:
        stmt = (
            select(TaxRate)
            .where(TaxRate.country == country, or_(TaxRate.region == region, TaxRate.region.is_(None)))
            .order_by(TaxRate.region.is_(None))  # regional rate wins over country-wide
            .limit(1)
        )
        rate = (await self._session.execute(stmt)).scalar_one_or_none()
        if rate is None:
            return TaxRule(rate_bps=0, inclusive=self._default_inclusive)
        return TaxRule(rate_bps=rate.rate_bps, inclusive=rate.inclusive)
