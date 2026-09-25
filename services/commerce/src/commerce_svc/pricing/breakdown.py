"""The money pipeline (docs §6.1) as PURE functions: no I/O, no clock, no randomness.

    lines → subtotal → promotions (allocated per line) → shipping → tax → total

Everything that touches money for a cart or quote goes through here, which is
what makes the invariants property-testable (see tests/test_breakdown.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from commerce_common.money import Money, sum_money


@dataclass(frozen=True, slots=True)
class PricedLine:
    key: str  # cart item id
    sku_id: str
    product_id: str
    name: str
    variant_label: str | None
    image_url: str | None
    quantity: int
    unit_price: Money
    compare_at: Money | None = None

    @property
    def total(self) -> Money:
        return self.unit_price.times(self.quantity)


@dataclass(frozen=True, slots=True)
class PromotionRule:
    promotion_id: str
    code: str | None
    label: str
    kind: Literal["percent", "fixed"]
    percent_bps: int | None = None
    amount: Money | None = None
    min_subtotal: Money | None = None


@dataclass(frozen=True, slots=True)
class ShippingRule:
    flat: Money
    free_from: Money | None = None


@dataclass(frozen=True, slots=True)
class TaxRule:
    rate_bps: int
    inclusive: bool


@dataclass(frozen=True, slots=True)
class AppliedDiscount:
    promotion_id: str
    code: str | None
    label: str
    amount: Money


@dataclass(frozen=True, slots=True)
class RejectedPromotion:
    promotion_id: str
    code: str | None
    reason: Literal["min_subtotal_not_met", "currency_mismatch", "no_discountable_amount"]
    min_subtotal: Money | None = None


@dataclass(frozen=True, slots=True)
class LineResult:
    line: PricedLine
    gross: Money  # unit × qty
    discount: Money  # this line's share of all discounts
    net: Money  # gross − discount
    tax: Money


@dataclass(slots=True)
class Breakdown:
    currency: str
    lines: list[LineResult]
    subtotal: Money
    discounts: list[AppliedDiscount]
    rejected: list[RejectedPromotion]
    discount_total: Money
    shipping: Money
    shipping_tax: Money
    tax_total: Money
    tax_inclusive: bool
    total: Money
    item_count: int = field(default=0)


def _eligible(rule: PromotionRule, currency: str, subtotal: Money) -> RejectedPromotion | None:
    for money in (rule.amount, rule.min_subtotal):
        if money is not None and money.currency != currency:
            return RejectedPromotion(rule.promotion_id, rule.code, "currency_mismatch")
    if rule.min_subtotal is not None and subtotal < rule.min_subtotal:
        return RejectedPromotion(rule.promotion_id, rule.code, "min_subtotal_not_met", rule.min_subtotal)
    return None


def apply_promotions(
    lines: list[PricedLine], rules: list[PromotionRule], currency: str
) -> tuple[list[Money], list[AppliedDiscount], list[RejectedPromotion]]:
    """Applies rules in order; each discount is allocated across lines by their remaining amount."""
    remaining = [line.total for line in lines]
    subtotal = sum_money(remaining, currency)
    applied: list[AppliedDiscount] = []
    rejected: list[RejectedPromotion] = []

    for rule in rules:
        if (reason := _eligible(rule, currency, subtotal)) is not None:
            rejected.append(reason)
            continue
        base = sum_money(remaining, currency)
        if base.is_zero:
            rejected.append(RejectedPromotion(rule.promotion_id, rule.code, "no_discountable_amount"))
            continue
        if rule.kind == "percent":
            amount = base.basis_points(rule.percent_bps or 0)
        else:
            assert rule.amount is not None
            amount = rule.amount.min(base)
        if amount.is_zero:
            continue
        shares = amount.allocate([r.amount_minor for r in remaining])
        remaining = [r - s for r, s in zip(remaining, shares, strict=True)]
        applied.append(AppliedDiscount(rule.promotion_id, rule.code, rule.label, amount))

    return remaining, applied, rejected


def compute(
    lines: list[PricedLine],
    rules: list[PromotionRule],
    currency: str,
    *,
    shipping: ShippingRule | None = None,
    tax: TaxRule | None = None,
) -> Breakdown:
    """Cart snapshot when ``shipping``/``tax`` are None; full quote when both are given."""
    if any(line.unit_price.currency != currency for line in lines):
        raise ValueError("all lines must be priced in the cart currency")

    zero = Money.zero(currency)
    subtotal = sum_money((line.total for line in lines), currency)
    net_lines, applied, rejected = apply_promotions(lines, rules, currency)
    net_total = sum_money(net_lines, currency)

    shipping_amount = zero
    if shipping is not None and lines:
        free = shipping.free_from is not None and shipping.free_from <= net_total
        shipping_amount = zero if free else shipping.flat

    tax_rule = tax or TaxRule(rate_bps=0, inclusive=False)

    def tax_of(amount: Money) -> Money:
        if tax_rule.inclusive:
            return amount.inclusive_tax_portion(tax_rule.rate_bps)
        return amount.basis_points(tax_rule.rate_bps)

    line_results = [
        LineResult(line=line, gross=line.total, discount=line.total - net, net=net, tax=tax_of(net))
        for line, net in zip(lines, net_lines, strict=True)
    ]
    shipping_tax = tax_of(shipping_amount)
    tax_total = sum_money((r.tax for r in line_results), currency) + shipping_tax

    total = net_total + shipping_amount
    if not tax_rule.inclusive:
        total = total + tax_total

    return Breakdown(
        currency=currency,
        lines=line_results,
        subtotal=subtotal,
        discounts=applied,
        rejected=rejected,
        discount_total=subtotal - net_total,
        shipping=shipping_amount,
        shipping_tax=shipping_tax,
        tax_total=tax_total,
        tax_inclusive=tax_rule.inclusive,
        total=total,
        item_count=sum(line.quantity for line in lines),
    )
