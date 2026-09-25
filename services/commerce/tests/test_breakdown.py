"""Property tests for the money pipeline: the invariants that must hold for ANY cart."""

from hypothesis import given, settings
from hypothesis import strategies as st

from commerce_common.money import Money, sum_money
from commerce_svc.pricing.breakdown import PricedLine, PromotionRule, ShippingRule, TaxRule, compute

CURRENCY = st.sampled_from(["USD", "JPY", "KWD", "EUR"])


@st.composite
def scenario(draw: st.DrawFn) -> tuple[str, list[PricedLine], list[PromotionRule], ShippingRule, TaxRule]:
    currency = draw(CURRENCY)
    n = draw(st.integers(min_value=1, max_value=8))
    lines = [
        PricedLine(
            key=f"l{i}",
            sku_id=f"s{i}",
            product_id=f"p{i}",
            name=f"item {i}",
            variant_label=None,
            image_url=None,
            quantity=draw(st.integers(min_value=1, max_value=20)),
            unit_price=Money(draw(st.integers(min_value=0, max_value=5_000_000)), currency),
        )
        for i in range(n)
    ]
    rules: list[PromotionRule] = []
    for i in range(draw(st.integers(min_value=0, max_value=3))):
        if draw(st.booleans()):
            rules.append(
                PromotionRule(f"pr{i}", f"P{i}", "pct", "percent", percent_bps=draw(st.integers(1, 10_000)))
            )
        else:
            amount = Money(draw(st.integers(1, 10_000_000)), currency)
            rules.append(PromotionRule(f"pr{i}", f"F{i}", "fixed", "fixed", amount=amount))
    shipping = ShippingRule(
        flat=Money(draw(st.integers(0, 50_000)), currency),
        free_from=draw(st.none() | st.builds(lambda v: Money(v, currency), st.integers(0, 10_000_000))),
    )
    tax = TaxRule(rate_bps=draw(st.integers(0, 3_000)), inclusive=draw(st.booleans()))
    return currency, lines, rules, shipping, tax


@settings(max_examples=400)
@given(scenario())
def test_quote_invariants(
    data: tuple[str, list[PricedLine], list[PromotionRule], ShippingRule, TaxRule],
) -> None:
    currency, lines, rules, shipping, tax = data
    b = compute(lines, rules, currency, shipping=shipping, tax=tax)
    zero = Money.zero(currency)

    # Everything is in one currency and never negative
    for m in (b.subtotal, b.discount_total, b.shipping, b.tax_total, b.total):
        assert m.currency == currency
        assert not m.is_negative

    # Subtotal is the exact sum of line totals
    assert b.subtotal == sum_money((line.total for line in lines), currency)

    # Discounts never exceed the subtotal, and per-line shares add up EXACTLY
    assert b.discount_total <= b.subtotal
    assert sum_money((r.discount for r in b.lines), currency) == b.discount_total
    assert sum_money((d.amount for d in b.discounts), currency) == b.discount_total
    assert all(r.net == r.gross - r.discount and not r.net.is_negative for r in b.lines)

    # Tax is the sum of per-line tax plus shipping tax
    assert b.tax_total == sum_money((r.tax for r in b.lines), currency) + b.shipping_tax

    # The total identity
    net = b.subtotal - b.discount_total
    expected = net + b.shipping if tax.inclusive else net + b.shipping + b.tax_total
    assert b.total == expected
    if tax.inclusive:
        assert b.tax_total <= b.total

    # Free-shipping threshold respected
    if shipping.free_from is not None and shipping.free_from <= net:
        assert b.shipping == zero


def test_worked_example_us_exclusive_tax() -> None:
    usd = lambda v: Money(v, "USD")  # noqa: E731
    lines = [PricedLine("l1", "s1", "p1", "Trail Runner GTX", None, None, 1, usd(12900))]
    promo = PromotionRule("pr1", "WELCOME10", "Welcome offer", "percent", percent_bps=1000)
    b = compute(lines, [promo], "USD", shipping=ShippingRule(usd(800), usd(10000)), tax=TaxRule(825, False))
    assert b.discount_total == usd(1290)  # 10 % of $129.00
    assert b.shipping == usd(0)  # $116.10 ≥ $100 → free
    assert b.tax_total == usd(958)  # 8.25 % of $116.10 = $9.578 → $9.58
    assert b.total == usd(12568)


def test_min_subtotal_rejection_is_reported() -> None:
    usd = lambda v: Money(v, "USD")  # noqa: E731
    lines = [PricedLine("l1", "s1", "p1", "Socks", None, None, 1, usd(2400))]
    promo = PromotionRule("pr1", "SAVE20", "$20 off $150", "fixed", amount=usd(2000), min_subtotal=usd(15000))
    b = compute(lines, [promo], "USD")
    assert b.discount_total == usd(0)
    assert b.rejected[0].reason == "min_subtotal_not_met"
